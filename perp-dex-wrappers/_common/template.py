from mpdex.base import MultiPerpDex, MultiPerpDexMixin

class Template(MultiPerpDexMixin,MultiPerpDex):
    def __init__(self):
        super().__init__()
    
    async def create_order(self, symbol, side, amount, price=None, order_type='market'):
        pass

    async def get_position(self, symbol):
        pass
    
    async def close_position(self, symbol, position, *, is_reduce_only=True):
        pass
    
    async def get_collateral(self):
        pass
    
    async def get_open_orders(self, symbol):
        pass
    
    async def cancel_orders(self, symbol):
        pass

    async def get_mark_price(self,symbol):
        pass

    async def close(self):
        pass