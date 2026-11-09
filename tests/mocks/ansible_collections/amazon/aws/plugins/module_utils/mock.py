"""Offline AWS contract doubles. Fail closed outside check mode; no SDK/network."""
from ansible.module_utils.basic import AnsibleModule


def run(kind):
    specs = {
        'ec2_vpc_subnet_info': dict(region='str', subnet_ids='list'),
        'ec2_security_group': dict(region='str', name='str', description='str', vpc_id='str', rules='list',
                                  rules_egress='list', purge_rules='bool', purge_rules_egress='bool'),
        'ec2_instance_info': dict(region='str', filters='dict'),
        'ec2_instance': dict(region='str', name='str', image_id='str', instance_type='str', instance_role='str',
                             state='str', wait='bool', termination_protection='bool', instance_initiated_shutdown_behavior='str',
                             network_interfaces='list', metadata_options='dict', volumes='list', tags='dict'),
    }
    module = AnsibleModule(argument_spec={k: {'type': v} for k, v in specs[kind].items()}, supports_check_mode=True)
    if not module.check_mode:
        module.fail_json(msg='AWS doubles may ONLY run in check mode')
    p = module.params
    if kind == 'ec2_vpc_subnet_info':
        module.exit_json(changed=False, subnets=[dict(subnet_id=s, availability_zone=f'us-east-1{chr(97+i)}',
                                                     vpc_id='vpc-0123456789abcdef0', map_public_ip_on_launch=False)
                                                for i, s in enumerate(p['subnet_ids'])])
    if kind == 'ec2_instance_info':
        module.exit_json(changed=False, instances=[])
    if kind == 'ec2_security_group':
        for rule in p['rules'] + p['rules_egress']:
            assert rule['proto'] == 'tcp'
            assert set(rule['ports']) <= {443, 8443, 9440, 9010, 9281, 9444}
            assert rule.get('cidr_ip') != '0.0.0.0/0'
        module.exit_json(changed=True, group_id='sg-0123456789abcdef0')
    assert p['metadata_options']['http_tokens'] == 'required'
    assert p['metadata_options']['http_put_response_hop_limit'] == 1
    assert p['termination_protection']
    assert len(p['network_interfaces']) == 1
    assert p['network_interfaces'][0]['assign_public_ip'] is False
    assert p['volumes'][0]['ebs']['encrypted'] is True
    assert p['volumes'][0]['ebs']['delete_on_termination'] is False
    assert p['volumes'][0]['ebs']['kms_key_id']
    module.exit_json(changed=True, instances=[])
