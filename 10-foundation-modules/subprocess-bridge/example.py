"""subprocess-bridge 사용 데모. 실제로 실행하려면 자식 측 exchange_bridge.py가 필요."""
import asyncio
from subprocess_wrapper import SubprocessExchangeWrapper


async def main():
    wrapper = SubprocessExchangeWrapper(
        venv_python="./lighter_venv/bin/python",   # Windows: lighter_venv\\Scripts\\python.exe
        exchange="lighter",
        config_path="config.yaml",
        account="lighter_main",
        bridge_module="strategies.exchange_bridge",
    )
    await wrapper.start()
    try:
        price = await wrapper.get_mark_price("BTC")
        print(f"BTC mark={price}")
        bal = await wrapper.get_balance()
        print(f"balance={bal}")
    finally:
        await wrapper.close()


asyncio.run(main())
