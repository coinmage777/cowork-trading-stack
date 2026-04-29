import requests
import json

with open('config.json', encoding='utf-8') as f:
    config = json.load(f)

brightdata = config['brightdata']
ip_test_list = config.get('ipTest', [])

def build_username(ip):
    username_template = brightdata["usernameTemplate"]
    return (
        username_template
        .replace("[[CUSTOMER_ID]]", brightdata["customerId"])
        .replace("[[ZONE]]", brightdata["zone"])
        .replace("[[IP]]", ip)
    )
    
def test_ip(ip):
    username = build_username(ip)
    print(username)
    password = brightdata["password"]
    host = brightdata["host"]
    port = brightdata["port"]
    
    proxies = {
        "http": f"http://{username}:{password}@{host}:{port}",
        "https": f"http://{username}:{password}@{host}:{port}",
    }

    try:
        print(ip)
        # geo 정보 요청
        geo = requests.get("https://geo.brdtest.com/mygeo.json", proxies=proxies, timeout=5).json()

        print(f"  → Country: {geo.get('country')}")
        print(f"  → City: {geo.get('geo', {}).get('city')}")
        print(f"  → ASN: {geo.get('asn', {}).get('org_name')}")
        print(f"  → Lat/Lng: {geo.get('geo', {}).get('latitude')}, {geo.get('geo', {}).get('longitude')}")
        print("-" * 60)

    except Exception as e:
        print(f"❌ IP: {ip} → 연결 실패: {e}")
        print("-" * 60)

# 테스트 실행
for ip in ip_test_list:
    test_ip(ip)
