"""GCP REST transport. Every client owns its credentials and frozen network route."""
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import urlsplit
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
COMPUTE = "https://compute.googleapis.com/compute/v1/projects/"
IMAGES = {"debian-12": ("debian-cloud", "debian-12"),
          "ubuntu-2404": ("ubuntu-os-cloud", "ubuntu-2404-lts-amd64")}


class GcpError(Exception):
    pass


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise GcpError("项目、可用区或资源名称格式无效")
    return value


def validate(project, credentials, proxy=""):
    segment(project)
    if not isinstance(credentials, dict) or credentials.get("type") != "service_account":
        raise GcpError("请上传 Google 服务账号的 JSON 密钥文件")
    email = credentials.get("client_email")
    if not isinstance(email, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com", email):
        raise GcpError("服务账号 client_email 无效")
    try:
        key = serialization.load_pem_private_key(credentials["private_key"].encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
    except Exception:
        raise GcpError("服务账号 RSA 私钥无效，至少需要 2048 位") from None
    if not isinstance(proxy, str):
        raise GcpError("代理地址必须为字符串")
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or \
                    parsed.path not in ("", "/") or parsed.query or parsed.fragment or \
                    (parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError()
        except ValueError:
            raise GcpError("GCP 代理配置无效，已阻止连接") from None
    return key


class Client:
    def __init__(self, account):
        self.project = segment(account["project_id"])
        credentials = json.loads(account["credentials"]) if isinstance(account["credentials"], str) else dict(account["credentials"])
        proxy = account.get("proxy_url", "")
        self.key = validate(self.project, credentials, proxy)
        self.email = credentials["client_email"]
        self.key_id = credentials.get("private_key_id", "")
        if proxy.startswith("socks5://"):
            proxy = "socks5h://" + proxy[len("socks5://"):]
        self.proxies = {"http": proxy, "https": proxy} if proxy else {}
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = dict(self.proxies)
        self.token = None
        self.expires = 0

    def __enter__(self): return self
    def __exit__(self, *_): self.session.close()

    def _send(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, proxies=dict(self.proxies),
                                            timeout=(15, 45), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise GcpError("GCP 代理连接失败，未回退直连" if self.proxies else "GCP 连接失败，请检查服务器网络") from None
        if 300 <= response.status_code < 400:
            raise GcpError("GCP 接口返回了重定向，已阻止转发凭据")
        try:
            data = response.json()
        except ValueError:
            raise GcpError(f"GCP 接口返回无效响应（HTTP {response.status_code}）") from None
        if not response.ok:
            labels = {401:"认证失败，请检查服务账号密钥", 403:"权限不足或 API 尚未启用", 404:"项目或资源不存在", 409:"资源状态冲突", 429:"请求或资源配额超限"}
            raise GcpError(f"GCP HTTP {response.status_code}：{labels.get(response.status_code, '云端请求失败，请查看 Google Cloud 控制台')}" )
        if not isinstance(data, dict): raise GcpError("GCP 响应格式无效")
        return data

    def _access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        now = int(time.time())
        def b64(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
        header = {"alg":"RS256", "typ":"JWT"}
        if self.key_id: header["kid"] = self.key_id
        claims = {"iss":self.email, "scope":"https://www.googleapis.com/auth/cloud-platform",
                  "aud":TOKEN_URL, "iat":now, "exp":now+3600}
        message = b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims).encode())
        signature = self.key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        result = self._send("POST", TOKEN_URL, data={"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion":message+"."+b64(signature)})
        token = result.get("access_token")
        if not isinstance(token, str) or not token: raise GcpError("Google 未返回访问令牌")
        self.token = token
        self.expires = time.time() + max(0, min(int(result.get("expires_in", 3600)), 3600)-60)
        return token

    def call(self, method, path, body=None, params=None, monitoring=False):
        base = f"https://monitoring.googleapis.com/v3/projects/{self.project}/" if monitoring else COMPUTE+self.project+"/"
        return self._send(method, base+path, headers={"Authorization":"Bearer "+self._access_token()}, json=body, params=params)

    def pages(self, path, params=None, monitoring=False):
        query = dict(params or {})
        seen = set()
        while True:
            data = self.call("GET", path, params=query, monitoring=monitoring)
            yield data
            token = data.get("nextPageToken")
            if not token: break
            if token in seen: raise GcpError("GCP 分页响应异常")
            seen.add(token)
            query["pageToken"] = token

    def instances(self):
        result = []
        for page in self.pages("aggregated/instances", {"returnPartialSuccess":"true"}):
            if page.get('unreachables'):
                raise GcpError('部分可用区读取失败，请稍后重试；未将不完整结果作为完整列表')
            for scope in page.get("items", {}).values():
                if scope.get('warning', {}).get('code') not in (None, 'NO_RESULTS_ON_PAGE'):
                    raise GcpError('部分可用区读取异常，请检查权限并重试')
                for item in scope.get("instances", []):
                    interfaces = item.get("networkInterfaces", [])
                    result.append({"id":str(item["id"]), "name":item["name"], "status":item.get("status"),
                        "zone":item.get("zone", "").rsplit("/",1)[-1],
                        "machine_type":item.get("machineType", "").rsplit("/",1)[-1],
                        "public_ip":next((c.get("natIP", "") for n in interfaces for c in n.get("accessConfigs", []) if c.get("natIP")), ""),
                        "private_ip":interfaces[0].get("networkIP", "") if interfaces else "", "created":item.get("creationTimestamp", ""),
                        "disks":[{"name":d.get("source", "").rsplit("/",1)[-1], "boot":d.get("boot",False), "auto_delete":d.get("autoDelete",False)} for d in item.get("disks", [])]})
        return sorted(result, key=lambda x:(x["zone"],x["name"]))

    def options(self, zone=None):
        if zone:
            return {"machine_types":[{"name":m["name"], "cpus":m.get("guestCpus"), "memory_mb":m.get("memoryMb")} for p in self.pages(f"zones/{segment(zone)}/machineTypes") for m in p.get("items", [])]}
        zones = [z["name"] for p in self.pages("zones") for z in p.get("items",[]) if z.get("status") == "UP"]
        networks = [dict(name=n["name"], automatic=n.get("autoCreateSubnetworks",False)) for p in self.pages("global/networks") for n in p.get("items",[])]
        subnets = [dict(name=s["name"], region=s["region"].rsplit("/",1)[-1], network=s["network"].rsplit("/",1)[-1]) for p in self.pages("aggregated/subnetworks") for scope in p.get("items",{}).values() for s in scope.get("subnetworks",[])]
        return {"zones":zones, "networks":networks, "subnets":subnets, "images":list(IMAGES)}

    def operation(self, zone, name):
        data = self.call("GET", f"zones/{segment(zone)}/operations/{segment(name)}")
        if data.get("error"):
            codes = ", ".join(str(e.get("code", "UNKNOWN")) for e in data["error"].get("errors",[]))
            raise GcpError("GCP 云端操作失败："+codes)
        return {"name":data.get("name"), "status":data.get("status"), "target_id":data.get("targetId")}

    def wait(self, zone, operation):
        deadline = time.monotonic()+60
        while True:
            state = self.operation(zone, operation["name"])
            if state["status"] == "DONE": return state
            if time.monotonic() >= deadline: raise GcpError("GCP 操作仍在执行，请刷新实例确认状态后再操作")
            time.sleep(1)

    def create(self, body):
        zone, name = segment(body["zone"]), segment(body["name"])
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", name): raise GcpError("实例名称需要以小写字母开头")
        if body["image"] not in IMAGES: raise GcpError("不支持的镜像")
        image_project, family = IMAGES[body["image"]]
        network = segment(body["network"])
        interface = {"network":f"projects/{self.project}/global/networks/{network}"}
        if body.get("subnet"):
            interface["subnetwork"] = f"projects/{self.project}/regions/{zone.rsplit('-',1)[0]}/subnetworks/{segment(body['subnet'])}"
        if body.get("public_ip", True): interface["accessConfigs"] = [{"name":"External NAT", "type":"ONE_TO_ONE_NAT", "networkTier":"PREMIUM"}]
        username = body["username"]
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username): raise GcpError("SSH 用户名格式无效")
        try:
            if '\n' in body["ssh_key"].strip() or '\r' in body["ssh_key"].strip(): raise ValueError()
            serialization.load_ssh_public_key(body["ssh_key"].strip().encode())
        except Exception: raise GcpError("SSH 公钥格式无效") from None
        request = {"name":name, "machineType":f"zones/{zone}/machineTypes/{segment(body['machine_type'])}",
            "disks":[{"boot":True,"autoDelete":False,"initializeParams":{"sourceImage":f"projects/{image_project}/global/images/family/{family}","diskSizeGb":body["disk_gb"],"diskType":f"zones/{zone}/diskTypes/pd-balanced"}}],
            "networkInterfaces":[interface], "serviceAccounts":[], "metadata":{"items":[{"key":"ssh-keys","value":username+":"+body["ssh_key"].strip()}, {"key":"block-project-ssh-keys","value":"true"}]}}
        # No VM service account or IAM permissions are implicitly granted.
        return self.call("POST", f"zones/{zone}/instances", request, {"requestId":str(uuid.uuid4())})

    def action(self, zone, name, action, preserve_disks=True):
        path = f"zones/{segment(zone)}/instances/{segment(name)}"
        if action in ("start", "stop", "reset"):
            return self.call("POST", path+"/"+action, params={"requestId":str(uuid.uuid4())})
        if action == "delete":
            params = {"requestId":str(uuid.uuid4())}
            if preserve_disks:
                instance = self.call("GET", path)
                for disk in instance.get("disks", []):
                    if disk.get("autoDelete"):
                        operation = self.call("POST", path+"/setDiskAutoDelete", params={"deviceName":disk["deviceName"], "autoDelete":"false", "requestId":str(uuid.uuid4())})
                        self.wait(zone, operation)
            return self.call("DELETE", path, params=params)
        if action != "change-ip": raise GcpError("不支持的实例操作")
        instance = self.call("GET", path)
        pairs = [(n,c) for n in instance.get("networkInterfaces",[]) for c in n.get("accessConfigs",[]) if c.get("type") == "ONE_TO_ONE_NAT"]
        if not pairs: raise GcpError("实例没有可更换的 IPv4 公网配置")
        nic, old = pairs[0]
        region = zone.rsplit("-",1)[0]
        for page in self.pages(f"regions/{segment(region)}/addresses", {"filter":'address = "'+old.get("natIP", "")+'"'}):
            if page.get("items"): raise GcpError("当前为保留/静态 IP，已阻止自动移除；请在 Google Cloud 控制台管理")
        operation = self.call("POST", path+"/deleteAccessConfig", params={"networkInterface":nic["name"],"accessConfig":old["name"]})
        self.wait(zone, operation)
        try:
            return self.call("POST", path+"/addAccessConfig", {"name":old["name"],"type":"ONE_TO_ONE_NAT","networkTier":old.get("networkTier","PREMIUM")}, {"networkInterface":nic["name"]})
        except GcpError as error:
            raise GcpError("旧公网配置已移除，新 IP 申请失败，请到 Google Cloud 控制台恢复公网配置："+str(error)) from None

    def traffic(self, days=90):
        now = datetime.now(timezone.utc)
        interval = {"interval.startTime":(now-timedelta(days=days)).isoformat(), "interval.endTime":now.isoformat(),
            "aggregation.alignmentPeriod":"86400s", "aggregation.perSeriesAligner":"ALIGN_SUM", "aggregation.crossSeriesReducer":"REDUCE_SUM",
            "aggregation.groupByFields":"resource.labels.instance_id", "view":"FULL", "pageSize":10000}
        totals = {}
        for direction, metric in (("rx_bytes","received_bytes_count"), ("tx_bytes","sent_bytes_count")):
            params = {**interval,"filter":f'metric.type="compute.googleapis.com/instance/network/{metric}" AND resource.type="gce_instance" AND resource.labels.project_id="{self.project}"'}
            for page in self.pages("timeSeries", params, monitoring=True):
                for series in page.get("timeSeries",[]):
                    identifier = series.get("resource",{}).get("labels",{}).get("instance_id", "unknown")
                    for point in series.get("points",[]):
                        date = point["interval"]["endTime"][:10]
                        value = point.get("value",{})
                        amount = int(value.get("int64Value", 0)) if "int64Value" in value else float(value.get("doubleValue",0))
                        row = totals.setdefault((date, identifier), {"date":date,"instance_id":identifier,"rx_bytes":0,"tx_bytes":0})
                        row[direction] += amount
        rows = sorted(totals.values(), key=lambda x:(x["date"],x["instance_id"]), reverse=True)
        return {"days":days,"rows":rows,"rx_bytes":sum(r["rx_bytes"] for r in rows),"tx_bytes":sum(r["tx_bytes"] for r in rows)}


def overview(instances):
    candidates = [x for x in instances if x["machine_type"] == "e2-micro" and x["zone"].rsplit("-",1)[0] in ("us-west1","us-central1","us-east1")]
    return {"total":len(instances),"running":sum(x["status"]=="RUNNING" for x in instances),"zones":dict(Counter(x["zone"] for x in instances)),"free_tier_candidates":len(candidates)}
