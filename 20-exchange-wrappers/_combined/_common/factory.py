import importlib  # [ADDED]

def _load(exchange_platform: str):  # [ADDED] 필요한 경우에만 모듈 로드
    mapping = {
        "paradex": ("mpdex.exchanges.paradex", "ParadexExchange"),
        "edgex": ("mpdex.exchanges.edgex", "EdgexExchange"),
        "lighter": ("mpdex.exchanges.lighter", "LighterExchange"),
        "grvt": ("mpdex.exchanges.grvt", "GrvtExchange"),
        "backpack": ("mpdex.exchanges.backpack", "BackpackExchange"),
        "treadfi.hyperliquid": ("mpdex.exchanges.treadfi_hl", "TreadfiHlExchange"),
        "treadfi.pacifica": ("mpdex.exchanges.treadfi_pc", "TreadfiPcExchange"),
        "variational": ("mpdex.exchanges.variational", "VariationalExchange"),
        "pacifica": ("mpdex.exchanges.pacifica", "PacificaExchange"),
        "hyperliquid": ("mpdex.exchanges.hyperliquid","HyperliquidExchange"),
        "superstack": ("mpdex.exchanges.superstack","SuperstackExchange"),
        "standx": ("mpdex.exchanges.standx", "StandXExchange"),
        "extended": ("mpdex.exchanges.extended", "ExtendedExchange"),
        "nado": ("mpdex.exchanges.nado", "NadoExchange"),
        "hotstuff": ("mpdex.exchanges.hotstuff", "HotstuffExchange"),
        "decibel": ("mpdex.exchanges.decibel", "DecibelExchange"),
        "ethereal": ("mpdex.exchanges.ethereal", "EtherealExchange"),
        "bulk": ("mpdex.exchanges.bulk", "BulkExchange"),
        "katana": ("mpdex.exchanges.katana", "KatanaExchange"),
        "ostium": ("mpdex.exchanges.ostium", "OstiumExchange"),
        "aster": ("mpdex.exchanges.aster", "AsterExchange"),
        "risex": ("mpdex.exchanges.risex", "RisexExchange"),
    }
    try:
        mod, cls = mapping[exchange_platform]
    except KeyError:
        raise ValueError(f"Unsupported exchange: {exchange_platform}")
    module = importlib.import_module(mod)
    return getattr(module, cls)

async def create_exchange(exchange_platform: str, key_params=None):  # [MODIFIED] 지연 로드 사용
    if key_params is None:
        raise ValueError(f"[ERROR] key_params is required for exchange: {exchange_platform}")
    Ex = _load(exchange_platform)  # [ADDED]

    if exchange_platform == "paradex":
        return await Ex(
            key_params.wallet_address,
            key_params.paradex_address,
            key_params.paradex_private_key
            ).init()

    elif exchange_platform == "edgex":
        return await Ex(
            key_params.account_id,
            key_params.private_key
            ).init()

    elif exchange_platform == "grvt":
        return await Ex(
            key_params.api_key,
            key_params.account_id,
            key_params.secret_key,
            use_ws=getattr(key_params, 'use_ws', True)
            ).init()

    elif exchange_platform == "backpack":
        return await Ex(
            key_params.api_key,
            key_params.secret_key
            ).init()

    elif exchange_platform == "lighter":
        import asyncio as _aio
        ex = Ex(
            key_params.account_id,
            key_params.private_key,
            key_params.api_key_id,
            key_params.l1_address
        )
        return await _aio.wait_for(ex.init(), timeout=30.0)

    elif exchange_platform == "treadfi.hyperliquid":
        return await Ex(
            key_params.session_cookies,
            key_params.login_wallet_address,
            key_params.login_wallet_private_key,
            key_params.trading_wallet_address,
            key_params.account_name,
            getattr(key_params,'trading_wallet_private_key',None),
            key_params.options if hasattr(key_params,"options") else None
            ).init()

    elif exchange_platform == "treadfi.pacifica":
        return await Ex(
            session_cookies=getattr(key_params, 'session_cookies', None),
            login_wallet_address=getattr(key_params, 'login_wallet_address', None),
            login_wallet_private_key=getattr(key_params, 'login_wallet_private_key', None),
            account_name=key_params.account_name,
            pacifica_public_key=getattr(key_params, 'pacifica_public_key', None),
            ).init()

    elif exchange_platform == "variational":
        return await Ex(
            key_params.evm_wallet_address,
            key_params.session_cookies,
            key_params.evm_private_key
            ).init()

    elif exchange_platform == "pacifica":
        return await Ex(
            key_params.public_key,
            key_params.agent_public_key,
            key_params.agent_private_key
            ).init()

    elif exchange_platform == "hyperliquid":
        return await Ex(
            wallet_address = key_params.wallet_address,
            wallet_private_key = key_params.wallet_private_key,
            agent_api_address = key_params.agent_api_address,
            agent_api_private_key = key_params.agent_api_private_key,
            by_agent = key_params.by_agent,
            vault_address = key_params.vault_address,
            builder_code = key_params.builder_code,
            builder_fee_pair = key_params.builder_fee_pair,
            FrontendMarket = key_params.FrontendMarket,
            proxy = getattr(key_params, 'proxy', None),
            cloid_prefix = getattr(key_params, 'cloid_prefix', None),
            builder_rotation = getattr(key_params, 'builder_rotation', None),
        ).init()

    elif exchange_platform == "superstack":
        return await Ex(
            wallet_address = key_params.wallet_address,
            api_key = key_params.api_key,
            vault_address = key_params.vault_address,
            builder_fee_pair = key_params.builder_fee_pair,
            FrontendMarket = key_params.FrontendMarket,
            proxy = getattr(key_params, 'proxy', None),
        ).init()

    elif exchange_platform == "standx":
        return await Ex(
            wallet_address = key_params.wallet_address,
            chain = getattr(key_params, 'chain', 'bsc'),
            evm_private_key = getattr(key_params, 'evm_private_key', None),
            session_token = getattr(key_params, 'session_token', None),
        ).init(
            login_port = getattr(key_params, 'login_port', 6969),
            open_browser = getattr(key_params, 'open_browser', True),
        )

    elif exchange_platform == "extended":
        return await Ex(
            api_key = key_params.api_key,
            public_key = key_params.stark_public_key,
            private_key = key_params.stark_private_key,
            vault = key_params.vault_id,
            network = getattr(key_params, 'network', 'mainnet'),
            prefer_ws = getattr(key_params, 'prefer_ws', True),
        ).init()

    elif exchange_platform == "nado":
        return await Ex(
            private_key = key_params.private_key,
            use_mainnet = getattr(key_params, 'use_mainnet', True),
        ).init()

    elif exchange_platform == "hotstuff":
        return await Ex(
            private_key = key_params.private_key,
            is_testnet = getattr(key_params, 'is_testnet', False),
        ).init()

    elif exchange_platform == "decibel":
        return await Ex(
            private_key = key_params.private_key,
            api_key = getattr(key_params, 'api_key', None),
            use_mainnet = getattr(key_params, 'use_mainnet', True),
            subaccount_address = getattr(key_params, 'subaccount_address', None),
        ).init()

    elif exchange_platform == "ethereal":
        return await Ex(
            private_key = key_params.private_key,
        ).init()

    elif exchange_platform == "bulk":
        return await Ex(
            private_key = key_params.private_key,
        ).init()

    elif exchange_platform == "katana":
        ex = Ex(
            api_key = key_params.api_key,
            api_secret = key_params.api_secret,
            wallet_private_key = key_params.wallet_private_key,
        )
        return ex

    elif exchange_platform == "ostium":
        return await Ex(
            private_key=key_params.private_key,
            rpc_url=getattr(key_params, 'rpc_url', None),
        ).init()

    elif exchange_platform == "aster":
        return await Ex(
            user_address=key_params.user_address,
            signer_private_key=key_params.signer_private_key,
        ).init()


    elif exchange_platform == "risex":
        return await Ex(
            wallet_address=key_params.wallet_address,
            private_key=key_params.private_key,
            base_url=getattr(key_params, "base_url", "https://api.testnet.rise.trade"),
            timeout=float(getattr(key_params, "timeout", 10.0)),
        ).init()

    else:
        raise ValueError(f"Unsupported exchange: {exchange_platform}")

SYMBOL_FORMATS = {
    "paradex":  lambda c, q=None: f"{c}-USD-PERP",
    "edgex":    lambda c, q=None: f"{c}USD",
    "grvt":     lambda c, q=None: f"{c}_USDT_Perp",
    "backpack": lambda c, q=None: f"{c}_USDC_PERP",
    "lighter":  lambda c, q=None: c,
    "treadfi.hyperliquid": lambda coin, q=None: (
        f"{coin.split(':')[0].lower()}_{coin.split(':')[1].upper()}:PERP-{q or 'USDC'}"
        if ":" in coin
        else f"{coin.upper()}:PERP-{q or 'USDC'}"
    ),
    "treadfi.pacifica": lambda coin, q=None: f"{coin.upper()}:PERP-{q or 'USDC'}",
    "variational": lambda coin, q=None: coin.upper(), # same
    "pacifica": lambda coin, q=None: coin.upper(), # same
    "hyperliquid": lambda coin, q=None: coin.upper(), # use internal mapping
    "dreamcash": lambda coin, q=None: coin.upper(), # HL frontend (builder code)
    "hyena": lambda coin, q=None: f"hyna:{coin.upper()}", # HyENA HIP-3 builder perp (USDe margin)
    "mass": lambda coin, q=None: coin.upper(), # HL frontend
    "lit": lambda coin, q=None: coin.upper(), # HL frontend
    "dexari": lambda coin, q=None: coin.upper(), # HL frontend
    "liquid": lambda coin, q=None: coin.upper(), # HL frontend
    "based": lambda coin, q=None: coin.upper(), # HL frontend
    "supercexy": lambda coin, q=None: coin.upper(), # HL frontend
    "bullpen": lambda coin, q=None: coin.upper(), # HL frontend
    "hl_wallet_b": lambda coin, q=None: coin.upper(), # HL wallet B group
    "superstack": lambda coin, q=None: coin.upper(), # use internal mapping
    "standx": lambda coin, q=None: f"{coin.upper()}-USD",  # BTC-USD
    "standx_2": lambda coin, q=None: f"{coin.upper()}-USD",  # BTC-USD
    "extended": lambda coin, q=None: f"{coin.upper()}-USD",  # BTC-USD
    "nado": lambda coin, q=None: coin.upper(),  # BTC, ETH (product_id로 내부 매핑)
    "nado_2": lambda coin, q=None: coin.upper(),  # nado wallet B
    "hyperliquid_2": lambda coin, q=None: coin.upper(),  # HL wallet A (miracle rotation)
    "hl_wallet_c": lambda coin, q=None: coin.upper(),  # HL wallet C (0xWALLC pair trading)
    "dreamcash_2": lambda coin, q=None: coin.upper(),  # DreamCash supersexy wallet
    "katana_2": lambda coin, q=None: f"{coin.upper()}-USD",  # Katana supersexy wallet
    "hyena_2": lambda coin, q=None: f"hyna:{coin.upper()}",  # HyENA supersexy wallet
    "variational_2": lambda coin, q=None: coin.upper(),  # Variational supersexy wallet
    "ostium": lambda coin, q=None: coin.upper(),  # BTC, ETH (pair index로 내부 매핑)
    "aster": lambda coin, q=None: f"{coin.upper()}USDT",  # BTCUSDT (Binance 호환)
    "hotstuff": lambda coin, q=None: f"{coin.upper()}-PERP",  # BTC-PERP
    "decibel": lambda coin, q=None: coin.upper(),  # BTC, ETH (market_name으로 내부 매핑)
    "ethereal": lambda coin, q=None: f"{coin.upper()}USD",  # BTCUSD, ETHUSD, SOLUSD
    "ethereal_2": lambda coin, q=None: f"{coin.upper()}USD",  # supersexy wallet
    "bulk": lambda coin, q=None: f"{coin.upper()}-USD",  # BTC-USD, ETH-USD
    "katana": lambda coin, q=None: f"{coin.upper()}-USD",  # BTC-USD, ETH-USD, SOL-USD
    "reya": lambda coin, q=None: f"{coin.upper()}RUSDPERP",  # BTCRUSDPERP, ETHRUSDPERP
    "risex": lambda coin, q=None: f"{coin.upper()}-PERP",  # BTC-PERP, ETH-PERP (Rise.trade testnet)
}

SPOT_SYMBOL_FORMATS = {
    "backpack": lambda c: f"{c[0]}_{c[1]}", # BTC_USDC 형태
    "lighter": lambda c: f"{c[0]}/{c[1]}", # BTC/USDC 형태
    "hyperliquid": lambda c: f"{c[0]}/{c[1]}", # BTC/USDC 형태
    "superstack": lambda c: f"{c[0]}/{c[1]}", # BTC/USDC 형태
    "treadfi.hyperliquid": lambda c: f"{c[0]}-{c[1]}", # BTC-USDC 형태
    "edgex": lambda c: f"{c[0]}/{c[1]}", # BTC/USDC 형태
}

def symbol_create(exchange_platform: str, coin: str, *, is_spot=False, quote=None):
    """spot의 경우 BTC/USDC와 같은 형태, quote가 있음"""
    """perp의 경우 BTC의 형태, dex가 붙으면 xyz:XYZ100 형태, quote가 없음"""

    if is_spot:
        # 총 3케이스를 다룸 "/", "_", "-"
        splitters = ['/','_','-']
        for spliter in splitters:
            if spliter in coin:
                base_symbol = coin.split(spliter)[0].upper()
                quote = coin.split(spliter)[1].upper()
                #print(base_symbol,quote)
                break
        try:
            return SPOT_SYMBOL_FORMATS[exchange_platform]([base_symbol,quote])
        except KeyError:
            raise ValueError(f"Unsupported exchange: {exchange_platform}, coin: {coin}")
    else:
        # perp가 default
        coin = coin.upper()
        try:
            return SYMBOL_FORMATS[exchange_platform](coin, quote)
        except KeyError:
            raise ValueError(f"Unsupported exchange: {exchange_platform}, coin: {coin}")
