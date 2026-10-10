"""AWS service models/signing, with an explicit per-account requests transport.

No SDK credential provider, default profile, environment endpoint or metadata lookup.
Only immutable SDK models are shared; credentials, HTTP pools and routes are not.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit

import botocore
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.loaders import Loader
from botocore.model import ServiceModel
from botocore.parsers import create_parser
from botocore.serialize import create_serializer
from botocore.exceptions import BotoCoreError
import requests


class AwsError(Exception):
    pass


def loader():
    # Do not load user models from ~/.aws/models or AWS_DATA_PATH.
    return Loader(extra_search_paths=[str(Path(botocore.__file__).parent / 'data')],
                  include_default_search_paths=False)


@lru_cache(maxsize=1)
def regions():
    partition = next(p for p in loader().load_data('endpoints')['partitions'] if p['partition'] == 'aws')
    available = partition['services']['lightsail']['endpoints']
    return [{'id':key, 'name':value['description']} for key,value in sorted(partition['regions'].items()) if key in available]


@lru_cache(maxsize=2)
def model(service):
    return ServiceModel(loader().load_service_model(service, 'service-2'))


def validate(region, credentials, proxy=''):
    if region not in {r['id'] for r in regions()}:
        raise AwsError('请选择受支持的 Lightsail 区域')
    if not isinstance(credentials, dict): raise AwsError('请填写 AWS Access Key 和 Secret Key')
    if not re.fullmatch(r'[A-Za-z0-9]{16,128}', credentials.get('access_key_id', '')):
        raise AwsError('AWS Access Key ID 格式无效')
    if not re.fullmatch(r'[^\s\x00-\x1f]{20,128}', credentials.get('secret_access_key', '')):
        raise AwsError('AWS Secret Access Key 格式无效')
    token = credentials.get('session_token', '')
    if not isinstance(token, str) or len(token) > 16384 or any(c.isspace() for c in token):
        raise AwsError('AWS Session Token 格式无效')
    if credentials['access_key_id'].startswith('ASIA') and not token:
        raise AwsError('临时 Access Key 必须填写对应 Session Token')
    if not isinstance(proxy, str): raise AwsError('代理地址必须为字符串')
    if proxy:
        try:
            p = urlsplit(proxy)
            if p.scheme not in ('http','https','socks5','socks5h') or not p.hostname or p.path not in ('','/') or p.query or p.fragment or (p.port is not None and not 1 <= p.port <= 65535):
                raise ValueError()
        except ValueError:
            raise AwsError('AWS 代理配置无效，已阻止连接') from None


class Client:
    def __init__(self, account):
        credentials = json.loads(account['credentials']) if isinstance(account['credentials'], str) else dict(account['credentials'])
        self.region = account['region']
        proxy = account.get('proxy_url', '')
        validate(self.region, credentials, proxy)
        self.credentials = Credentials(credentials['access_key_id'], credentials['secret_access_key'], credentials.get('session_token') or None)
        if proxy.startswith('socks5://'): proxy = 'socks5h://' + proxy[len('socks5://'):]
        self.proxies = {'http':proxy, 'https':proxy} if proxy else {}
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = dict(self.proxies)

    def __enter__(self): return self
    def __exit__(self, *_): self.session.close()

    def call(self, service, operation, **params):
        service_model = model(service)
        op = service_model.operation_model(operation)
        try:
            serialized = create_serializer(service_model.protocol).serialize_to_request(params, op)
            signing_name = service_model.metadata.get('signingName', service_model.endpoint_prefix)
            url = f'https://{service_model.endpoint_prefix}.{self.region}.amazonaws.com' + serialized['url_path']
            query = serialized.get('query_string')
            if query: url += '?' + (urlencode(query) if isinstance(query, dict) else query)
            request = AWSRequest(method=serialized['method'], url=url, data=serialized['body'], headers=serialized['headers'])
            SigV4Auth(self.credentials, signing_name, self.region).add_auth(request)
            prepared = request.prepare()
            response = self.session.request(prepared.method, prepared.url, data=prepared.body,
                headers=dict(prepared.headers), proxies=dict(self.proxies), timeout=(15,45), allow_redirects=False)
        except requests.RequestException:
            raise AwsError('AWS 代理连接失败，未回退直连' if self.proxies else 'AWS 连接失败，请检查服务器网络') from None
        except BotoCoreError:
            raise AwsError('AWS 请求参数无效，请检查资源选择') from None
        if 300 <= response.status_code < 400:
            raise AwsError('AWS 返回重定向，已阻止转发凭据')
        try:
            parsed = create_parser(service_model.protocol).parse(
                {'body':response.content,'headers':{k.lower():v for k,v in response.headers.items()},'status_code':response.status_code}, op.output_shape)
        except Exception:
            raise AwsError(f'AWS 返回无法解析的响应（HTTP {response.status_code}）') from None
        if not response.ok or parsed.get('Error'):
            code = parsed.get('Error', {}).get('Code', 'Unknown')
            labels = {'AccessDenied':'当前密钥权限不足', 'UnauthenticatedException':'AWS 凭据无效',
                'AccountSetupInProgressException':'Lightsail 账号正在初始化，请稍后重试',
                'RegionSetupInProgressException':'Lightsail 区域正在初始化，请稍后重试',
                'InvalidInputException':'参数无效，请检查镜像、套餐、可用区和名称',
                'NotFoundException':'当前区域资源不存在', 'OperationFailureException':'Lightsail 操作失败，请在 AWS 控制台核对',
                'AccessDeniedException':'当前密钥权限不足', 'AuthFailure':'密钥无效或区域未启用',
                'InvalidClientTokenId':'Access Key 或 Session Token 无效', 'ExpiredToken':'临时凭据已过期',
                'ExpiredTokenException':'临时凭据已过期', 'SignatureDoesNotMatch':'签名不匹配，请检查 Secret Key 和服务器时间',
                'RequestExpired':'请求已过期，请校准服务器时间', 'RequestLimitExceeded':'请求频率超限，请稍后重试',
                'InstanceLimitExceeded':'实例数量配额不足', 'InsufficientInstanceCapacity':'当前可用区容量不足',
                'IncorrectInstanceState':'实例状态不允许此操作', 'InvalidInstanceID.NotFound':'实例不存在或不属于当前区域'}
            # Never return the AWS error message: it can echo supplied secrets/parameters.
            safe_code = code if re.fullmatch(r'[A-Za-z0-9._-]{1,100}', str(code)) else 'Unknown'
            raise AwsError(f'AWS HTTP {response.status_code} · {safe_code}：{labels.get(code, "云端请求失败，请检查 IAM 权限、区域和资源参数") }')
        return parsed

    def pages(self, operation, result_key, **params):
        seen = set()
        while True:
            page = self.call('lightsail', operation, **params)
            yield from page.get(result_key, [])
            token = page.get('nextPageToken')
            if not token: break
            if token in seen: raise AwsError('AWS 分页响应异常')
            seen.add(token)
            params['pageToken'] = token

    def identity(self):
        result = self.call('sts', 'GetCallerIdentity')
        return {'account_id':result['Account'], 'arn':result['Arn'], 'region':self.region}

    def instances(self):
        result = []
        for item in self.pages('GetInstances','instances'):
            hardware = item.get('hardware',{})
            result.append({'id':item['arn'],'name':item['name'],'status':item.get('state',{}).get('name','unknown'),
                'zone':item.get('location',{}).get('availabilityZone',''), 'machine_type':item.get('bundleId',''),
                'blueprint':item.get('blueprintName',item.get('blueprintId','')), 'cpus':hardware.get('cpuCount'),
                'memory_gb':hardware.get('ramSizeInGb'), 'username':item.get('username',''), 'key_name':item.get('sshKeyName',''),
                'public_ip':item.get('publicIpAddress',''), 'private_ip':item.get('privateIpAddress',''),
                'ipv6':item.get('ipv6Addresses',[]),'static_ip':item.get('isStaticIp',False),
                'disks':[{'name':d.get('name',d.get('path','')),'size_gb':d.get('sizeInGb'), 'system':d.get('isSystemDisk',False)} for d in hardware.get('disks',[])],
                'created':str(item.get('createdAt',''))})
        return sorted(result,key=lambda i:(i['zone'],i['name']))

    def options(self):
        regions = self.call('lightsail','GetRegions',includeAvailabilityZones=True).get('regions',[])
        region = next((r for r in regions if r['name']==self.region),None)
        if not region: raise AwsError('Lightsail 未启用当前区域')
        return {'zones':[z['zoneName'] for z in region.get('availabilityZones',[]) if z.get('state')=='available'],
            'blueprints':[{'id':b['blueprintId'],'name':b['name'],'platform':b['platform'],'min_power':b.get('minPower',0)} for b in self.pages('GetBlueprints','blueprints',includeInactive=False) if b.get('isActive') and b.get('platform')=='LINUX_UNIX'],
            'bundles':[{'id':b['bundleId'],'name':b['name'],'cpus':b.get('cpuCount'), 'memory_gb':b.get('ramSizeInGb'),
                'disk_gb':b.get('diskSizeInGb'),'transfer_gb':b.get('transferPerMonthInGb'),'price':b.get('price'),
                'power':b.get('power',0),'platforms':b.get('supportedPlatforms',[]),'ipv4_count':b.get('publicIpv4AddressCount',1)}
                for b in self.pages('GetBundles','bundles',includeInactive=False) if b.get('isActive') and 'LINUX_UNIX' in b.get('supportedPlatforms',[])],
            'key_pairs':[k['name'] for k in self.pages('GetKeyPairs','keyPairs',includeDefaultKeyPair=True)]}

    def create(self, body):
        name = resource_name(body['name'])
        options = self.options()
        blueprint = next((b for b in options['blueprints'] if b['id']==body['blueprint_id']),None)
        bundle = next((b for b in options['bundles'] if b['id']==body['bundle_id']),None)
        if not blueprint or not bundle or bundle['power'] < blueprint['min_power']:
            raise AwsError('镜像或套餐无效，或套餐规格不足以运行所选镜像')
        if body['zone'] not in options['zones']: raise AwsError('可用区不属于当前账号区域')
        if body['key_name'] not in options['key_pairs']: raise AwsError('请选择当前区域的 SSH 密钥对')
        if body['ip_type']=='dualstack' and bundle['ipv4_count']<1:
            raise AwsError('所选套餐不含公网 IPv4，请使用仅 IPv6 或更换套餐')
        return operation_result(self.call('lightsail','CreateInstances',instanceNames=[name], availabilityZone=body['zone'],
            blueprintId=body['blueprint_id'],bundleId=body['bundle_id'],keyPairName=body['key_name'],ipAddressType=body['ip_type']))

    def action(self, name, action):
        name = resource_name(name)
        operations = {'start':'StartInstance','stop':'StopInstance','reboot':'RebootInstance','delete':'DeleteInstance'}
        if action not in operations: raise AwsError('不支持的 Lightsail 实例操作')
        # DeleteInstance removes the system disk; never describe it as preserved.
        return operation_result(self.call('lightsail',operations[action],instanceName=name))

    def operation(self, operation_id):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,128}',operation_id): raise AwsError('操作编号无效')
        op = self.call('lightsail','GetOperation',operationId=operation_id)['operation']
        if op.get('status')=='Failed':
            code = op.get('errorCode','Unknown')
            safe = code if re.fullmatch(r'[A-Za-z0-9._-]{1,100}',str(code)) else 'Unknown'
            raise AwsError('Lightsail 云端操作失败：'+safe+'，请在 AWS 控制台核对')
        return {'id':op['id'],'status':op.get('status','Unknown'),'terminal':op.get('isTerminal',False)}

    def metrics(self, name, days=7):
        name = resource_name(name)
        self.call('lightsail','GetInstance',instanceName=name)
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=days)
        period = 3600 if days<=7 else 86400
        rows = {}
        for metric,field,statistic,unit in [('NetworkIn','rx_bytes','Sum','Bytes'),('NetworkOut','tx_bytes','Sum','Bytes'),('CPUUtilization','cpu','Average','Percent')]:
            result = self.call('lightsail','GetInstanceMetricData',instanceName=name,metricName=metric,period=period,
                startTime=start,endTime=end,unit=unit,statistics=[statistic])
            for point in result.get('metricData',[]):
                stamp = point['timestamp'].astimezone(timezone.utc).isoformat()
                rows.setdefault(stamp,{'timestamp':stamp,'rx_bytes':None,'tx_bytes':None,'cpu':None})[field] = point.get(statistic.lower())
        data = sorted(rows.values(),key=lambda r:r['timestamp'],reverse=True)
        return {'instance_name':name,'days':days,'period_seconds':period,'rows':data,
            'rx_bytes':sum(r['rx_bytes'] or 0 for r in data),'tx_bytes':sum(r['tx_bytes'] or 0 for r in data)}

    def snapshots(self):
        return [{'name':s['name'],'state':s.get('state',''),'source':s.get('fromInstanceName',''),
            'size_gb':s.get('sizeInGb'),'created':str(s.get('createdAt',''))} for s in self.pages('GetInstanceSnapshots','instanceSnapshots')]

    def snapshot(self, name, snapshot_name):
        return operation_result(self.call('lightsail','CreateInstanceSnapshot',instanceName=resource_name(name),instanceSnapshotName=resource_name(snapshot_name)))

    def delete_snapshot(self, name):
        return operation_result(self.call('lightsail','DeleteInstanceSnapshot',instanceSnapshotName=resource_name(name)))

    def static_ips(self):
        return [{'name':s['name'],'ip':s.get('ipAddress',''),'attached_to':s.get('attachedTo',''),'attached':s.get('isAttached',False)} for s in self.pages('GetStaticIps','staticIps')]

    def static_ip_action(self, name, action, instance=''):
        name = resource_name(name)
        if action=='allocate': return operation_result(self.call('lightsail','AllocateStaticIp',staticIpName=name))
        if action=='attach':
            static = self.call('lightsail','GetStaticIp',staticIpName=name)['staticIp']
            if static.get('isAttached') and static.get('attachedTo')!=instance:
                raise AwsError('静态 IP 已绑定其他实例，已阻止自动解绑；请先核对并手动解绑')
            return operation_result(self.call('lightsail','AttachStaticIp',staticIpName=name,instanceName=resource_name(instance)))
        if action=='detach': return operation_result(self.call('lightsail','DetachStaticIp',staticIpName=name))
        if action=='release':
            static = self.call('lightsail','GetStaticIp',staticIpName=name)['staticIp']
            if static.get('isAttached'): raise AwsError('请先解绑静态 IP，再释放')
            return operation_result(self.call('lightsail','ReleaseStaticIp',staticIpName=name))
        raise AwsError('不支持的静态 IP 操作')


def resource_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,253}[A-Za-z0-9_]',value):
        raise AwsError('Lightsail 资源名称需为 2–255 位字母、数字、下划线或短横线，首尾不能为短横线')
    return value


def operation_result(result):
    return {'operations':[{'id':op['id'],'status':op.get('status','Unknown'),'terminal':op.get('isTerminal',False)} for op in result.get('operations',[])]}
