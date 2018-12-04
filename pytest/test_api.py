#!/usr/bin/env python3

import json
import time
import pytest
from conftest import Server

cluster = [
    Server(
        alias = 'router',
        instance_uuid = 'aaaaaaaa-aaaa-4000-b000-000000000001',
        replicaset_uuid = 'aaaaaaaa-0000-4000-b000-000000000000',
        roles = ['vshard-router'],
        binary_port = 33001,
        http_port = 8081,
    ),
    Server(
        alias = 'storage',
        instance_uuid = 'bbbbbbbb-bbbb-4000-b000-000000000001',
        replicaset_uuid = 'bbbbbbbb-0000-4000-b000-000000000000',
        roles = ['vshard-storage'],
        binary_port = 33002,
        http_port = 8082,
    ),
    Server(
        alias = 'expelled',
        instance_uuid = 'cccccccc-cccc-4000-b000-000000000001',
        replicaset_uuid = 'cccccccc-0000-4000-b000-000000000000',
        roles = [],
        binary_port = 33009,
        http_port = 8089,
    )
]

@pytest.fixture(scope="module")
def expelled(cluster):
    cluster['expelled'].kill()
    obj = cluster['router'].graphql("""
        mutation {
            expell_server(
                uuid: "cccccccc-cccc-4000-b000-000000000001"
            )
        }
    """)
    assert 'errors' not in obj

def test_self(cluster):
    obj = cluster['router'].graphql("""
        {
            cluster {
                self {
                    uri
                    uuid
                    alias
                }
            }
        }
    """)

    server_self = obj['data']['cluster']['self']
    assert server_self == {
        'uri': 'localhost:33001',
        'uuid': 'aaaaaaaa-aaaa-4000-b000-000000000001',
        'alias': 'router',
    }

def test_custom_http_endpoint(cluster):
    resp = cluster['router'].get('/custom-get')
    assert resp == 'GET OK'
    resp = cluster['router'].post('/custom-post')
    assert resp == 'POST OK'

def test_servers(cluster, expelled, helpers):
    obj = cluster['router'].graphql("""
        {
            servers {
                uri
                replicaset { roles }
            }
        }
    """)

    servers = obj['data']['servers']
    assert {
        'uri': 'localhost:33001',
        'replicaset': {'roles': ['vshard-router']}
    } == helpers.find(servers, 'uri', 'localhost:33001')
    assert {
        'uri': 'localhost:33002',
        'replicaset': {'roles': ['vshard-storage']}
    } == helpers.find(servers, 'uri', 'localhost:33002')
    assert len(servers) == 2

def test_replicasets(cluster, expelled, helpers):
    obj = cluster['router'].graphql("""
        {
            replicasets {
                uuid
                roles
                status
                master { uuid }
                servers { uri }
            }
        }
    """)

    replicasets = obj['data']['replicasets']
    assert {
        'uuid': 'aaaaaaaa-0000-4000-b000-000000000000',
        'roles': ['vshard-router'],
        'status': 'healthy',
        'master': {'uuid': 'aaaaaaaa-aaaa-4000-b000-000000000001'},
        'servers': [{'uri': 'localhost:33001'}]
    } == helpers.find(replicasets, 'uuid', 'aaaaaaaa-0000-4000-b000-000000000000')
    assert {
        'uuid': 'bbbbbbbb-0000-4000-b000-000000000000',
        'roles': ['vshard-storage'],
        'status': 'healthy',
        'master': {'uuid': 'bbbbbbbb-bbbb-4000-b000-000000000001'},
        'servers': [{'uri': 'localhost:33002'}]
    } == helpers.find(replicasets, 'uuid', 'bbbbbbbb-0000-4000-b000-000000000000')
    assert len(replicasets) == 2

def test_probe_server(cluster, expelled, module_tmpdir, helpers):
    srv = cluster['router']
    req = """mutation($uri: String!) { probe_server(uri:$uri) }"""

    obj = srv.graphql(req,
        variables={'uri': 'localhost:9'}
    )
    assert obj['errors'][0]['message'] == \
        'Probe "localhost:9" failed: no responce'

    obj = srv.graphql(req,
        variables={'uri': 'bad-host'}
    )
    assert obj['errors'][0]['message'] == \
        'Probe "bad-host" failed: ping was not sent'

    obj = srv.graphql(req,
        variables={'uri': srv.advertise_uri}
    )
    assert obj['data']['probe_server'] == True

def test_edit_server(cluster, expelled):
    obj = cluster['router'].graphql("""
        mutation {
            edit_server(
                uuid: "aaaaaaaa-aaaa-4000-b000-000000000001"
                uri: "localhost:3303"
            )
        }
    """)
    assert obj['errors'][0]['message'] == \
        'Server "localhost:3303" is not in membership'

    obj = cluster['router'].graphql("""
        mutation {
            edit_server(
                uuid: "cccccccc-cccc-4000-b000-000000000001"
                uri: "localhost:3303"
            )
        }
    """)
    assert obj['errors'][0]['message'] == \
        'Server "cccccccc-cccc-4000-b000-000000000001" is expelled'

    obj = cluster['router'].graphql("""
        mutation {
            edit_server(
                uuid: "dddddddd-dddd-4000-b000-000000000001"
                uri: "localhost:3303"
            )
        }
    """)
    assert obj['errors'][0]['message'] == \
        'Server "dddddddd-dddd-4000-b000-000000000001" not in config'

def test_edit_replicaset(cluster, expelled):
    obj = cluster['router'].graphql("""
        mutation {
            edit_replicaset(
                uuid: "bbbbbbbb-0000-4000-b000-000000000000"
                roles: ["vshard-router", "vshard-storage"]
            )
        }
    """)
    assert 'errors' not in obj

    obj = cluster['router'].graphql("""
        mutation {
            edit_replicaset(
                uuid: "bbbbbbbb-0000-4000-b000-000000000000"
                master: "bbbbbbbb-bbbb-4000-b000-000000000002"
            )
        }
    """)
    assert obj['errors'][0]['message'] == \
        'replicasets[bbbbbbbb-0000-4000-b000-000000000000].master does not exist'

    obj = cluster['storage'].graphql("""
        {
            replicasets(uuid: "bbbbbbbb-0000-4000-b000-000000000000") {
                uuid
                roles
                status
                servers { uri }
            }
        }
    """)

    replicasets = obj['data']['replicasets']
    assert len(replicasets) == 1
    assert {
        'uuid': 'bbbbbbbb-0000-4000-b000-000000000000',
        'roles': ['vshard-storage', 'vshard-router'],
        'status': 'healthy',
        'servers': [{'uri': 'localhost:33002'}]
    } == replicasets[0]

def test_uninitialized(module_tmpdir, helpers):
    srv = Server(
        binary_port = 33101,
        http_port = 8181,
        alias = 'dummy'
    )
    srv.start(
        workdir="{}/localhost-{}".format(module_tmpdir, srv.binary_port),
    )

    try:
        helpers.wait_for(srv.ping_udp, timeout=5)

        obj = srv.graphql("""
            {
                servers {
                    uri
                    replicaset { roles }
                }
                replicasets {
                    status
                }
                cluster {
                    self {
                        uri
                        uuid
                        alias
                    }
                }
            }
        """)

        servers = obj['data']['servers']
        assert len(servers) == 1
        assert servers[0] == {'uri': 'localhost:33101'}

        replicasets = obj['data']['replicasets']
        assert len(replicasets) == 0

        server_self = obj['data']['cluster']['self']
        assert server_self == {'uri': 'localhost:33101', 'alias': 'dummy'}

        obj = srv.graphql("""
            mutation {
                join_server(uri: "localhost:33001)")
            }
        """)
        assert obj['errors'][0]['message'] == \
            'Invalid attempt to call join_server()' + \
            ' on instance which is not bootstrapped yet.\n' + \
            'Call join_server with uri="localhost:33101" to bootstrap'

        obj = srv.graphql("""
            {
                cluster { failover }
            }
        """)
        assert 'errors' not in obj, obj['errors'][0]['message']
        assert obj['data']['cluster']['failover'] == False

        obj = srv.graphql("""
            mutation {
                cluster { failover(enabled: false) }
            }
        """)
        assert obj['errors'][0]['message'] == 'Not bootstrapped yet'
    finally:
        srv.kill()

def test_join_server_fail(cluster, expelled, module_tmpdir, helpers):
    srv = Server(
        binary_port = 33003,
        http_port = 8083,
    )
    srv.start(
        workdir="{}/localhost-{}".format(module_tmpdir, srv.binary_port),
    )

    try:
        helpers.wait_for(srv.ping_udp, timeout=5)

        obj = cluster['router'].graphql("""
            mutation {
                probe_server(
                    uri: "localhost:33003"
                )
            }
        """)
        assert 'errors' not in obj
        assert obj['data']['probe_server'] == True

        obj = cluster['router'].graphql("""
            mutation {
                join_server(
                    uri: "localhost:33003"
                    instance_uuid: "cccccccc-cccc-4000-b000-000000000001"
                )
            }
        """)
        assert obj['errors'][0]['message'] == \
            'Server "cccccccc-cccc-4000-b000-000000000001" is already joined'

    finally:
        srv.kill()

def test_join_server_good(cluster, expelled, module_tmpdir, helpers):
    srv = Server(
        binary_port = 33003,
        http_port = 8083,
    )
    srv.start(
        workdir="{}/localhost-{}".format(module_tmpdir, srv.binary_port)
    )

    try:
        helpers.wait_for(srv.ping_udp, timeout=5)

        obj = cluster['router'].graphql("""
            mutation {
                probe_server(uri: "localhost:33003")
            }
        """)
        assert 'errors' not in obj
        assert obj['data']['probe_server'] == True


        obj = cluster['router'].graphql("""
            mutation {
                join_server(
                    uri: "localhost:33003"
                    instance_uuid: "dddddddd-dddd-4000-b000-000000000001"
                    replicaset_uuid: "dddddddd-0000-4000-b000-000000000000"
                    roles: []
                )
            }
        """)
        assert 'errors' not in obj
        assert obj['data']['join_server'] == True

        helpers.wait_for(srv.connect, timeout=5)
        helpers.wait_for(cluster['router'].connect, timeout=5)

        obj = cluster['router'].graphql("""
            {
                servers {
                    uri
                    uuid
                    status
                    replicaset { uuid status roles }
                }
            }
        """)

        assert 'errors' not in obj
        servers = obj['data']['servers']
        assert len(servers) == 3
        assert {
            'uri': 'localhost:33003',
            'uuid': 'dddddddd-dddd-4000-b000-000000000001',
            'status': 'healthy',
            'replicaset': {
                'uuid': 'dddddddd-0000-4000-b000-000000000000',
                'roles': [],
                'status': 'healthy',
            }
        } == helpers.find(servers, 'uuid', 'dddddddd-dddd-4000-b000-000000000001')

    finally:
        srv.kill()
