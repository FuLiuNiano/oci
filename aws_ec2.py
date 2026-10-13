"""EC2 and account queries through the existing isolated AWS transport."""
import re
from datetime import datetime, timedelta, timezone

from aws_service import Client as AwsClient, AwsError


def identifier(value, prefix):
    if not re.fullmatch(prefix + r'-[a-f0-9]{8,32}', value):
        raise AwsError('AWS 资源编号格式无效')
    return value


class Client(AwsClient):
    def paged(self, service, operation, key, **params):
        seen = set()
        while True:
            page = self.call(service, operation, **params)
            yield from page.get(key, [])
            token = page.get('NextPageToken' if service == 'ce' else 'NextToken')
            if not token:
                return
            if token in seen:
                raise AwsError('AWS 分页响应异常')
            seen.add(token)
            params['NextPageToken' if service == 'ce' else 'NextToken'] = token

    def instances(self):
        rows = []
        for reservation in self.paged('ec2', 'DescribeInstances', 'Reservations'):
            for item in reservation.get('Instances', []):
                rows.append({'id':item['InstanceId'], 'name':next((t['Value'] for t in item.get('Tags', []) if t['Key']=='Name'), item['InstanceId']),
                    'status':item['State']['Name'], 'zone':item.get('Placement', {}).get('AvailabilityZone',''),
                    'machine_type':item['InstanceType'], 'public_ip':item.get('PublicIpAddress',''),
                    'private_ip':item.get('PrivateIpAddress',''), 'key_name':item.get('KeyName',''),
                    'ipv6':[a['Ipv6Address'] for n in item.get('NetworkInterfaces',[]) for a in n.get('Ipv6Addresses',[])],
                    'image':item.get('ImageId',''), 'created':str(item.get('LaunchTime',''))})
        return rows

    def options(self):
        return {'subnets':[{'id':s['SubnetId'], 'zone':s['AvailabilityZone'], 'vpc':s['VpcId']} for s in self.paged('ec2','DescribeSubnets','Subnets')],
            'groups':[{'id':g['GroupId'], 'name':g['GroupName'], 'vpc':g['VpcId']} for g in self.paged('ec2','DescribeSecurityGroups','SecurityGroups')],
            'keys':[k['KeyName'] for k in self.paged('ec2','DescribeKeyPairs','KeyPairs')]}

    def create(self, body):
        identifier(body['image'], 'ami')
        identifier(body['subnet'], 'subnet')
        groups = body['groups']
        if not groups or len(groups)>5:
            raise AwsError('请选择 1 至 5 个安全组')
        for group in groups:
            identifier(group, 'sg')
        options = self.options()
        subnet = next((s for s in options['subnets'] if s['id']==body['subnet']), None)
        if not subnet or body['key_name'] not in options['keys']:
            raise AwsError('子网或密钥对不属于当前账号区域')
        if any(not any(g['id']==group and g['vpc']==subnet['vpc'] for g in options['groups']) for group in groups):
            raise AwsError('安全组必须属于所选子网的 VPC')
        images = self.call('ec2','DescribeImages',ImageIds=[body['image']]).get('Images', [])
        types = self.call('ec2','DescribeInstanceTypes',InstanceTypes=[body['instance_type']]).get('InstanceTypes', [])
        if not images or images[0].get('State')!='available' or not types or images[0].get('Architecture') not in types[0].get('ProcessorInfo',{}).get('SupportedArchitectures',[]):
            raise AwsError('镜像不可用或架构与实例规格不兼容')
        result = self.call('ec2','RunInstances',ImageId=body['image'],InstanceType=body['instance_type'],
            MinCount=1,MaxCount=1,ClientToken=body['client_token'],KeyName=body['key_name'],
            NetworkInterfaces=[{'DeviceIndex':0,'SubnetId':body['subnet'],'Groups':groups,'AssociatePublicIpAddress':body['public_ipv4']}],
            MetadataOptions={'HttpTokens':'required'},
            TagSpecifications=[{'ResourceType':'instance','Tags':[{'Key':'Name','Value':body['name']}]}])
        return {'instance_ids':[i['InstanceId'] for i in result.get('Instances',[])], 'message':'创建请求已提交，请刷新核对状态；实例及磁盘可能产生费用'}

    def action(self, instance_id, action):
        identifier(instance_id, 'i')
        self.call('ec2','DescribeInstances',InstanceIds=[instance_id])
        op = {'start':'StartInstances','stop':'StopInstances','reboot':'RebootInstances','delete':'TerminateInstances'}.get(action)
        if not op:
            raise AwsError('不支持的 EC2 操作')
        self.call('ec2',op,InstanceIds=[instance_id])
        return {'message':'操作已提交，请刷新核对；终止实例可能按磁盘自动删除设置删除磁盘'}

    def metrics(self, instance_id, days=7):
        identifier(instance_id,'i')
        self.call('ec2','DescribeInstances',InstanceIds=[instance_id])
        end = datetime.now(timezone.utc)
        rows = {}
        for metric, field, statistic in [('NetworkIn','rx_bytes','Sum'),('NetworkOut','tx_bytes','Sum'),('CPUUtilization','cpu','Average')]:
            data = self.call('cloudwatch','GetMetricStatistics',Namespace='AWS/EC2',MetricName=metric,
                Dimensions=[{'Name':'InstanceId','Value':instance_id}],StartTime=end-timedelta(days=days),EndTime=end,
                Period=86400 if days>7 else 3600,Statistics=[statistic])
            for p in data.get('Datapoints',[]):
                stamp = p['Timestamp'].isoformat()
                rows.setdefault(stamp,{'time':stamp})[field] = p.get(statistic)
        return {'days':days,'rows':sorted(rows.values(),key=lambda r:r['time']),
            'rx_bytes':sum(r.get('rx_bytes',0) or 0 for r in rows.values()),'tx_bytes':sum(r.get('tx_bytes',0) or 0 for r in rows.values())}

    def quotas(self, service='ec2'):
        if service not in ('ec2','lightsail'):
            raise AwsError('不支持的配额服务')
        return [{'name':q['QuotaName'],'code':q['QuotaCode'],'value':q['Value'],'unit':q.get('Unit',''),
                 'adjustable':q.get('Adjustable',False)} for q in self.paged('service-quotas','ListServiceQuotas','Quotas',ServiceCode=service)]

    def costs(self, days=90):
        end = datetime.now(timezone.utc).date()
        # Billing query is account-wide; never label it as one region or profile's resources.
        rows = list(self.paged('ce','GetCostAndUsage','ResultsByTime',TimePeriod={'Start':str(end-timedelta(days=days)),'End':str(end)},
            Granularity='DAILY',Metrics=['UnblendedCost']))
        return {'scope':'AWS 结算账号全部服务与区域（按 IAM 可见范围）','days':days,
            'rows':[{'date':r['TimePeriod']['Start'],'amount':r['Total']['UnblendedCost']['Amount'],
                     'unit':r['Total']['UnblendedCost']['Unit'],'estimated':r.get('Estimated',False)} for r in rows]}
