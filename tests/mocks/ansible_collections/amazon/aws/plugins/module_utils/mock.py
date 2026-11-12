"""Offline AWS contract doubles. Fail closed outside check mode; no SDK/network."""
import os
import json
from ansible.module_utils.basic import AnsibleModule


def run(kind):
    specs = {
        'cloudformation': dict(stack_name='str', region='str', state='str', capabilities='list', template_body='str', termination_protection='bool'),
        'ec2_vpc_subnet_info': dict(region='str', subnet_ids='list'),
        'ec2_security_group': dict(region='str', name='str', description='str', vpc_id='str', rules='list',
                                  rules_egress='list', purge_rules='bool', purge_rules_egress='bool'),
        'ec2_vpc_route_table_info': dict(region='str', filters='dict'),
        'ec2_instance_info': dict(region='str', filters='dict'),
        'ec2_instance': dict(region='str', name='str', image_id='str', instance_type='str', instance_role='str',
                             state='str', wait='bool', termination_protection='bool', instance_initiated_shutdown_behavior='str',
                             network_interfaces='list', metadata_options='dict', volumes='list', tags='dict'),
    }
    module = AnsibleModule(argument_spec={k: {'type': v} for k, v in specs[kind].items()}, supports_check_mode=True)
    if not module.check_mode:
        module.fail_json(msg='AWS doubles may ONLY run in check mode')
    p = module.params
    if kind == 'cloudformation':
        resources = json.loads(p['template_body'])['Resources']
        bucket = resources['Bucket']['Properties']
        assert bucket['VersioningConfiguration']['Status'] == 'Enabled'
        assert all(bucket['PublicAccessBlockConfiguration'].values())
        encryption = bucket['BucketEncryption']['ServerSideEncryptionConfiguration'][0]
        assert encryption['ServerSideEncryptionByDefault']['SSEAlgorithm'] == 'aws:kms'
        assert encryption['ServerSideEncryptionByDefault']['KMSMasterKeyID'] == {'Fn::GetAtt': ['Key', 'Arn']}
        assert encryption['BucketKeyEnabled'] is False
        assert resources['Key']['Properties']['EnableKeyRotation']
        if bucket.get('ObjectLockEnabled'):
            assert bucket['ObjectLockConfiguration'] == {
                'ObjectLockEnabled': 'Enabled', 'Rule': {'DefaultRetention': {'Mode': 'GOVERNANCE', 'Days': 30}}}
        for name in ['Key', 'Bucket']:
            assert resources[name]['DeletionPolicy'] == resources[name]['UpdateReplacePolicy'] == 'Retain'
        rules = bucket['LifecycleConfiguration']['Rules']
        assert rules == [{'Id': 'AbortIncompleteMultipart', 'Status': 'Enabled', 'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7}}]
        policy = resources['BucketPolicy']['Properties']['PolicyDocument']['Statement']
        assert policy[0]['Effect'] == 'Deny' and policy[0]['Condition']['Bool']['aws:SecureTransport'] == 'false'
        assert {statement['Sid'] for statement in policy} == {
            'TLSOnly', 'RefuseExplicitNonKMS', 'RefuseWrongKey', 'RefuseKMSWithoutExplicitKey', 'RefuseCustomerEncryptionKey'}
        iam = resources['InstancePolicy']['Properties']
        assert iam['Roles'] == ['offline-clickhouse-role']
        statements = iam['PolicyDocument']['Statement']
        assert statements[0]['Condition']['StringLike']['s3:prefix'] == ['clickhouse/*']
        assert statements[1]['Resource'] == {'Fn::Sub': '${Bucket.Arn}/clickhouse/*'}
        assert set(statements[1]['Action']) == {'s3:GetObject', 's3:GetObjectVersion', 's3:PutObject', 's3:DeleteObject',
                                              's3:DeleteObjectVersion', 's3:AbortMultipartUpload'}
        assert statements[2]['Action'] == ['kms:GenerateDataKey', 'kms:Decrypt']
        assert statements[2]['Resource'] == {'Fn::GetAtt': ['Key', 'Arn']}
        assert p['termination_protection']
        module.exit_json(changed=True)
    if kind == 'ec2_vpc_subnet_info':
        module.exit_json(changed=False, subnets=[dict(subnet_id=s, availability_zone=f'us-east-1{chr(97+i)}',
                                                     vpc_id='vpc-0123456789abcdef0', map_public_ip_on_launch=False)
                                                for i, s in enumerate(p['subnet_ids'])])
    if kind == 'ec2_vpc_route_table_info':
        if os.environ.get('STACK_MOCK_MAIN_ROUTE') and 'association.subnet-id' in p['filters']:
            module.exit_json(changed=False, route_tables=[])
        gateway = 'igw-0123456789abcdef0' if os.environ.get('STACK_MOCK_PUBLIC_SUBNET') else 'local'
        module.exit_json(changed=False, route_tables=[{'routes': [{'gateway_id': gateway}]}])
    if kind == 'ec2_instance_info':
        if os.environ.get('STACK_MOCK_EXISTING'):
            node = p['filters']['tag:Name'].removeprefix('clickhouse-')
            zone = int(node[-1])
            role = node.startswith('keeper')
            module.exit_json(changed=False, instances=[dict(
                instance_id='i-0123456789abcdef0', image_id='ami-0123456789abcdef0', instance_type='m7i.large',
                private_ip_address=f'10.0.{zone}.{10 if role else 11}', subnet_id=f'subnet-{zone:017d}',
                iam_instance_profile={'arn': 'arn:aws:iam::000000000000:instance-profile/' +
                                      ('offline-test-profile' if role else 'offline-clickhouse-profile')},
                state={'name': 'running'}, metadata_options={'http_tokens': 'required', 'http_put_response_hop_limit': 1},
                security_groups=[{'group_id': 'sg-00000000000000001' if role else 'sg-00000000000000002'}])])
        module.exit_json(changed=False, instances=[])
    if kind == 'ec2_security_group':
        for rule in p['rules'] + p['rules_egress']:
            assert rule['proto'] == 'tcp'
            assert set(rule['ports']) <= {443, 8443, 9440, 9010, 9281, 9444}
            assert rule.get('cidr_ip') != '0.0.0.0/0'
        module.exit_json(changed=True, group_id='sg-00000000000000001' if p['name'].endswith('-keeper') else 'sg-00000000000000002')
    assert not os.environ.get('STACK_MOCK_EXISTING'), 'Rerun must not recreate an existing VM'
    assert p['metadata_options']['http_tokens'] == 'required'
    assert p['metadata_options']['http_put_response_hop_limit'] == 1
    assert p['termination_protection']
    assert p['instance_role'] == ('offline-clickhouse-profile' if p['tags']['Role'] == 'clickhouse' else 'offline-test-profile')
    assert len(p['network_interfaces']) == 1
    assert p['network_interfaces'][0]['assign_public_ip'] is False
    assert p['network_interfaces'][0]['groups'] == ['sg-00000000000000001' if p['tags']['Role'] == 'keeper' else 'sg-00000000000000002']
    assert p['volumes'][0]['ebs']['encrypted'] is True
    assert p['volumes'][0]['ebs']['delete_on_termination'] is False
    assert p['volumes'][0]['ebs']['kms_key_id']
    module.exit_json(changed=True, instances=[])
