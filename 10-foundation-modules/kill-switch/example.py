"""kill-switch 사용 데모. 빈 파일 하나로 진입 차단."""
from kill_switch import KillSwitch

ks = KillSwitch(data_dir="./data")

# 외부에서 (셸에서): touch ./data/KILL_lighter
ks.engage("lighter", reason="manual: API 점검")

# 봇 루프 안에서
ok, reason = ks.check("lighter")
print(f"lighter ok={ok} reason={reason}")  # ok=False reason=KILL_lighter

ok, reason = ks.check("nado")
print(f"nado    ok={ok} reason={reason}")   # ok=True

# 활성 목록
print("active:", ks.list_active())

# 해제
ks.release("lighter")
print("after release:", ks.list_active())
