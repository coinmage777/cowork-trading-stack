"""trigger-watcher 사용 데모."""
import asyncio
from trigger_watcher import TriggerWatcher


async def my_restart():
    print("graceful restart...")
    # cleanup, save state, exit. systemd가 다시 띄움.

async def my_reload():
    print("config reload (재시작 없이)")

async def my_close():
    print("전체 청산 + 종료")

def my_status_dump() -> dict:
    return {"positions": 3, "balance_usd": 1234.56, "live": True}


async def main():
    tw = TriggerWatcher(
        base_dir=".",
        on_restart=my_restart,
        on_reload=my_reload,
        on_close=my_close,
        status_dumper=my_status_dump,
        poll_interval=1.0,
    )
    # 외부에서: touch triggers/restart.trigger
    await tw.run()


asyncio.run(main())
