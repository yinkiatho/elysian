import requests
import json
import os


ASTER_FUTURES_BASE = "https://fapi.asterdex.com"  # replace with actual Aster base URL
ASTER_SPOT_BASE    = "https://sapi.asterdex.com"   # replace with actual Aster base URL

def fetch_aster_symbols():
    # --- Futures ---
    futures_data = requests.get(f"{ASTER_FUTURES_BASE}/fapi/v1/exchangeInfo").json()

    futures_symbols = [
        s["symbol"] for s in futures_data["symbols"]
        if s["status"] == "TRADING"
        and s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
    ]
    futures_tokens = list({
        token
        for s in futures_data["symbols"]
        if s["status"] == "TRADING"
        for token in [s["baseAsset"], s["quoteAsset"]]
    })

    # --- Spot ---
    spot_data = requests.get(f"{ASTER_SPOT_BASE}/api/v1/exchangeInfo").json()

    spot_symbols = [
        s["symbol"] for s in spot_data["symbols"]
        if s["status"] == "TRADING"
        and s["quoteAsset"] == "USDT"
    ]
    spot_tokens = list({
        token
        for s in spot_data["symbols"]
        if s["status"] == "TRADING"
        for token in [s["baseAsset"], s["quoteAsset"]]
    })

    return {
        "spot_symbols": spot_symbols,
        "spot_pairs": spot_symbols,
        "futures_symbols": futures_symbols,
        "futures_pairs": futures_symbols,
        "spot_tokens": spot_tokens,
        "futures_tokens": futures_tokens,
    }


def build_config(output_path: str):
    """Update a configuration with the latest Aster symbol data and write it out.

    ``base_config`` may be either a path to a JSON file or a dictionary object.
    When a path is provided the file is read; when a dict is given a shallow copy
    is made so the caller's structure isn't mutated.

    The updated configuration is written to ``output_path`` and also returned.
    """

    config = {}

    aster = fetch_aster_symbols()

    config["Spot Tokens"] = aster["spot_symbols"] 
    config["Futures Tokens"] = aster["futures_symbols"]
    

    with open(output_path, "w") as f:
        json.dump(config, f, indent=4)

    print(f"Config written to {output_path}")
    print(f"  Spot symbols    : {len(aster['spot_symbols'])}")
    print(f"  Futures symbols : {len(aster['futures_symbols'])}")

    return config



# When this module is executed directly we still provide a simple
# demonstration of how to update a file-based configuration.  Other
# callers can import ``build_config`` and pass in a dict if they already
# have one constructed.
if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    build_config("aster_exchange_info.json")

