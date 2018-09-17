#!/usr/bin/env tarantool
-- luacheck: globals box

local log = require('log')
local fio = require('fio')
local yaml = require('yaml')
local fiber = require('fiber')
local errors = require('errors')
local checks = require('checks')
local vshard = require('vshard')
local membership = require('membership')

local vars = require('cluster.vars').new('cluster.confapplier')
local pool = require('cluster.pool')
local utils = require('cluster.utils')
local topology = require('cluster.topology')
local service_registry = require('cluster.service-registry')

local e_yaml = errors.new_class('Parsing yaml failed')
local e_atomic = errors.new_class('Atomic call failed')
local e_config_load = errors.new_class('Loading configuration failed')
local e_config_fetch = errors.new_class('Fetching configuration failed')
local e_config_apply = errors.new_class('Applying configuration failed')
local e_config_validate = errors.new_class('Invalid config')

vars:new('conf')
vars:new('locks', {})
vars:new('applier_fiber', nil)
vars:new('applier_channel', nil)

local function load_from_file(filename)
    checks('string')
    if not utils.file_exists(filename) then
        return nil, e_config_load:new('file %q does not exist', filename)
    end

    local raw, err = utils.file_read(filename)
    if not raw then
        return nil, err
    end

    local confdir = fio.dirname(filename)

    local conf, err = e_yaml:pcall(yaml.decode, raw)
    if not conf then
        if not err then
            return nil, e_config_load:new('file %q is empty', filename)
        end

        return nil, err
    end

    local function _load(tbl)
        for k, v in pairs(tbl) do
            if type(v) == 'table' then
                local err
                if v['__file'] then
                    tbl[k], err = utils.file_read(confdir .. '/' .. v['__file'])
                else
                    tbl[k], err = _load(v)
                end
                if err then
                    return nil, err
                end
            end
        end
        return tbl
    end

    local conf, err = _load(conf)

    return conf, err
end

local function get_current(workdir)
    if vars.conf ~= nil then
        return table.deepcopy(vars.conf)
    end

    if not workdir then
        -- box was not configured yet
        return nil, e_config_load:new(
            "Failed to load config, because box.cfg hasn't been called yet")
    end

    local conf, err = load_from_file(
        utils.pathjoin(workdir, 'config.yml')
    )

    if not conf then
        log.error('%s', err)
        return nil, err
    end

    vars.conf = table.deepcopy(conf)
    topology.set(conf.servers)
    return conf
end

local function fetch_from_uri(uri)
    local conn, err = pool.connect(uri)
    if conn == nil then
        return nil, err
    end

    return conn:eval('return package.loaded["cluster.confapplier"].get_current()')
end

local function fetch_from_membership()
    local conf = get_current()
    if conf then
        if conf.servers[box.info.uuid] == nil
        or conf.servers[box.info.uuid] == 'expelled'
        or utils.table_count(conf.servers) == 1
        then
            return conf
        end
    end

    local candidates = {}
    for uri, member in membership.pairs() do
        if (member.status ~= 'alive') -- ignore non-alive members
        or (member.payload.uuid == nil)  -- ignore non-configured members
        or (member.payload.error ~= nil) -- ignore misconfigured members
        or (conf and member.payload.uuid == box.info.uuid) -- ignore myself
        or (conf and conf.servers[member.payload.uuid] == nil) -- ignore aliens
        then
            -- ignore that member
        else

            table.insert(candidates, uri)
        end
    end

    if #candidates == 0 then
        return nil
    end

    return e_config_fetch:pcall(fetch_from_uri, candidates[math.random(#candidates)])
end

-- Perform all checks, and answer the question: "Can I apply the given config?"
-- on success return instance_uuid, nil
-- on failure raise error (which will be catched in validate() function)
local function validate(conf_new)
    e_config_validate:assert(
        type(conf_new) == 'table',
        'config must be a table'
    )
    e_config_validate:assert(
        type(conf_new.servers) == 'table',
        'servers must be a table, got %s', type(conf_new.servers)
    )

    local conf_old = nil
    local myself_uuid = nil
    if type(box.cfg) == 'function' then
        -- box.cfg was not configured yet
        conf_old = {}

        -- find myself by uri:
        local myself_uri = membership.myself().uri
        for uuid, server in pairs(conf_new.servers) do
            if server.uri == myself_uri then
                myself_uuid = uuid
                break
            end
        end

        e_config_validate:assert(
            myself_uuid ~= nil,
            'instance is not in the config'
        )
    else
        conf_old = vars.conf
        myself_uuid = box.info.uuid
    end

    e_config_validate:assert(
        topology.validate(conf_new.servers, conf_old.servers)
    )

    return myself_uuid
end

local function _apply(channel)
    while true do
        local conf = unpack(channel:get())
        if not conf then
            return
        end

        vars.conf = conf
        topology.set(conf.servers)

        local replication = topology.get_replication_config(
            box.info.cluster.uuid
        )
        log.info('Setting replication to %s', table.concat(replication, ', '))
        local _, err = e_config_apply:pcall(box.cfg, {
            replication = replication,
        })
        if err then
            log.error('%s', err)
        end

        local roles = conf.servers[box.info.uuid].roles

        if utils.table_find(roles, 'vshard-storage') then
            vshard.storage.cfg({
                sharding = topology.get_sharding_config(),
                bucket_count = conf.bucket_count,
            }, box.info.uuid)
            service_registry.set('vshard-storage', vshard.storage)

            -- local srv = storage.new()
            -- srv:apply_config(conf)
        end

        if utils.table_find(roles, 'vshard-router') then
            -- local srv = ibcore.server.new()
            -- srv:apply_config(conf)
            -- service_registry.set('ib-core', srv)
            vshard.router.cfg({
                sharding = topology.get_sharding_config(),
                bucket_count = conf.bucket_count,
            })
            service_registry.set('vshard-router', vshard.router)
        end
    end
end

local function apply(conf)
    -- called by:
    -- 1. bootstrap.init_roles
    -- 2. clusterwide
    checks('table')

    if not vars.applier_channel then
        vars.applier_channel = fiber.channel(1)
    end

    if not vars.applier_fiber then
        vars.applier_fiber = fiber.create(_apply, vars.applier_channel)
        vars.applier_fiber:name('cluster.confapplier')
    end

    while not vars.applier_channel:has_readers() do
        -- TODO should we specify timeout here?
        if vars.applier_fiber:status() == 'dead' then
            return nil, e_config_apply:new('impossible due to previous error')
        end
        fiber.sleep(0)
    end

    local ok, err = utils.file_write(
        utils.pathjoin(box.cfg.memtx_dir, 'config.yml'),
        yaml.encode(conf)
    )

    if not ok then
        return nil, err
    end

    vars.applier_channel:put(conf)
    fiber.yield()
    return true
end

local function _clusterwide(conf_new)
    checks('table')

    local ok, err = validate(conf_new)
    if not ok then
        return nil, err
    end

    local conf_old, err = get_current()
    if not conf_old then
        return nil, err
    end

    local servers_new = conf_new.servers
    local servers_old = cluster_topology.get()

    local configured_uri_list = {}
    for uuid, _ in pairs(servers_new) do
        if servers_new[uuid] == 'expelled' then
            -- ignore expelled servers
        elseif servers_old[uuid] == nil then
            -- new servers bootstrap themselves through membership
            -- dont call nex.box on them
        else
            local uri = servers_new[uuid].uri
            local conn, err = pool.connect(uri)
            if conn == nil then
                return nil, err
            end
            local ok, err = e_config_validate:pcall(
                conn.call,
                conn,
                'confapplier.validate',
                {conf_new}
            )
            if not ok then
                return nil, err
            end
            configured_uri_list[uri] = false
        end
    end

    local _apply_error = nil
    for uri, _ in pairs(configured_uri_list) do
        local conn, err = pool.connect(uri)
        if conn == nil then
            return nil, err
        end
        log.info('Applying config on %s', uri)
        local ok, err = apply_error:pcall(
            conn.call,
            conn,
            'confapplier.apply',
            {conf_new}
        )
        configured_uri_list[uri] = true

        if not ok then
            log.error('%s', err)
            _apply_error = err
            break
        end
    end

    if not _apply_error then
        local sharding_config = cluster_topology.get_sharding_config()

        if utils.table_count(sharding_config) > 0 then

            while not vshard_utils.is_vshard_ready_for_bootstrap() do
                fiber.sleep(0.1)
            end

            log.info('Bootstrapping vshard.router from config applier...')

            local ok, err = vshard.router.bootstrap({timeout=10})
            -- NON_EMPTY means that the cluster has already been initialized,
            -- and this failure is expected
            if not ok and err.code ~= vshard.error.code['NON_EMPTY'] then
                return nil, err
            end
        end

        return true
    end

    for uri, configured in pairs(configured_uri_list) do
        if configured then
            log.info('Rollback config on %s', uri)
            local conn, err = pool.connect(uri)
            if conn == nil then
                return nil, err
            end
            local ok, err = rollback_error:pcall(
                conn.call,
                conn,
                'confapplier.apply',
                {conf_old}
            )
            if not ok then
                log.error(err)
            end
        end
    end

    return nil, _apply_error
end

local function clusterwide(conf_new)
    if vars.locks['clusterwide'] == true  then
        return nil, e_atomic:new('confapplier.clusterwide is already running')
    end

    box.session.su('admin')
    vars.locks['clusterwide'] = true
    local ok, err = e_config_apply:pcall(_clusterwide, conf_new)
    vars.locks['clusterwide'] = false

    return ok, err
end

return {
    get_current = get_current,
    load_from_file = load_from_file,
    fetch_from_membership = fetch_from_membership,

    validate = function(conf)
        return e_config_validate:pcall(validate, conf)
    end,
    apply = apply,
    clusterwide = clusterwide,
}
