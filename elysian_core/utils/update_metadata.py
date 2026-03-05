import requests


# Function to get coin metadata using suix_getCoinMetadata
def get_coin_metadata(coin_type):
    url = "https://sui.blockpi.network/v1/rpc/98fc01fa640d7bde33e7ff6c1119145fc305b7cb"
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "suix_getCoinMetadata",
        "params": [coin_type]
    }

    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        result = response.json().get("result")
        return result
    else:
        print(f"Error: {response.status_code}")
        return None

# Function to update dictionaries based on metadata
def update_dictionaries(token_addresses):
    for address in token_addresses:
        metadata = get_coin_metadata(address)
        
        if metadata:
            symbol = metadata['symbol']
            decimals = metadata['decimals']
            
            # Update DECIMALS dictionary
            DECIMALS[symbol] = decimals
            
            # Update UNIT_CONVERTER dictionary
            UNIT_CONVERTER[symbol] = 10 ** decimals
            
            # Update ASSET_TO_ADDRESS and ADDRESS_TO_ASSET dictionaries
            ASSET_TO_ADDRESS[symbol] = address
            ADDRESS_TO_ASSET[address] = symbol
        else:
            print(f"Metadata not found for address: {address}")

# Example token addresses to update
token_addresses = [ i for i in coinAddresses if i is not None]

# Run the update
update_dictionaries(token_addresses)

# Print updated dictionaries
print("Updated DECIMALS:", DECIMALS)
print("Updated UNIT_CONVERTER:", UNIT_CONVERTER)
print("Updated ASSET_TO_ADDRESS:", ASSET_TO_ADDRESS)
print("Updated ADDRESS_TO_ASSET:", ADDRESS_TO_ASSET)
