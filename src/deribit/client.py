import os
import time
import httpx
from datetime import datetime, UTC
from typing import Dict, Any, Optional


class DeribitClient:
    def __init__(self, testnet: bool = True):
        self.testnet = testnet
        self.base_url = "https://test.deribit.com/api/v2" if testnet else "https://www.deribit.com/api/v2"
        self.client_id = os.getenv("DERIBIT_API_KEY")
        self.client_secret = os.getenv("DERIBIT_SECRET_KEY")
        self.access_token: Optional[str] = None
        self.token_expiry: float = 0
        self._client = httpx.AsyncClient(timeout=10.0)

    async def aclose(self):
        await self._client.aclose()

    async def authenticate(self):
        # Read lazily so load_dotenv() called in main() is respected even though
        # DeribitClient is instantiated at module level (before load_dotenv runs).
        if not self.client_id:
            self.client_id = os.getenv("DERIBIT_API_KEY")
        if not self.client_secret:
            self.client_secret = os.getenv("DERIBIT_SECRET_KEY")
        if not self.client_id or not self.client_secret:
            raise ValueError("Deribit API credentials not found in environment.")

        response = await self._client.get(
            f"{self.base_url}/public/auth",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        res_data = response.json()
        if "result" in res_data:
            result = res_data["result"]
            self.access_token = result.get("access_token")
            expires_in = result.get("expires_in", 3600)
            self.token_expiry = time.time() + expires_in - 60
        else:
            raise Exception(f"Authentication failed: {res_data}")

    async def _get(self, endpoint: str, params: Dict[str, Any] = None, private: bool = False) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = {}
        if private:
            if not self.access_token or time.time() > self.token_expiry:
                await self.authenticate()
            headers["Authorization"] = f"Bearer {self.access_token}"

        response = await self._client.get(url, params=params, headers=headers)
        res_json = response.json()

        # Handle token expiration during request
        if private and (response.status_code == 401 or (isinstance(res_json, dict) and res_json.get("error", {}).get("code") == 13009)):
            await self.authenticate()
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = await self._client.get(url, params=params, headers=headers)
            res_json = response.json()

        if response.status_code != 200:
            raise Exception(f"Deribit API error: {response.status_code} - {response.text}")

        if "error" in res_json:
            raise Exception(f"Deribit API error: {res_json['error']}")
        return res_json["result"]

    async def _post(self, endpoint: str, data: Dict[str, Any] = None, private: bool = True) -> Dict[str, Any]:
        if private:
            if not self.access_token or time.time() > self.token_expiry:
                await self.authenticate()

        headers = {"Content-Type": "application/json"}
        if private:
            headers["Authorization"] = f"Bearer {self.access_token}"

        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time()),
            "method": endpoint,
            "params": data or {},
        }

        response = await self._client.post(self.base_url, json=payload, headers=headers)
        res_json = response.json()

        # Handle token expiration during request
        if private and (response.status_code == 401 or (isinstance(res_json, dict) and res_json.get("error", {}).get("code") == 13009)):
            await self.authenticate()
            headers["Authorization"] = f"Bearer {self.access_token}"
            payload["id"] = int(time.time())
            response = await self._client.post(self.base_url, json=payload, headers=headers)
            res_json = response.json()

        if response.status_code != 200:
            raise Exception(f"Deribit API error: {response.status_code} - {response.text}")

        if "error" in res_json:
            raise Exception(f"Deribit API error: {res_json['error']}")
        return res_json["result"]

    async def get_btc_spot_price(self) -> float:
        data = await self._get("public/get_index_price", {"index_name": "btc_usd"})
        return data["index_price"]

    async def get_dvol(self) -> float:
        """Fetch current BTC DVOL. Always uses mainnet (public endpoint)."""
        end = int(time.time() * 1000)
        start = end - 86400000
        mainnet_url = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

        response = await self._client.get(
            mainnet_url,
            params={"currency": "BTC", "resolution": "1D", "start_timestamp": start, "end_timestamp": end},
        )
        response.raise_for_status()
        data = response.json()
        if "result" in data and "data" in data["result"] and data["result"]["data"]:
            return data["result"]["data"][-1][4]  # close of last DVOL candle

        raise Exception("Could not fetch DVOL data from mainnet.")

    async def get_open_positions(self) -> list:
        data = await self._get("private/get_positions", {"currency": "BTC", "kind": "option"}, private=True)
        return data

    async def get_account_summary(self) -> Dict[str, Any]:
        return await self._get("private/get_account_summary", {"currency": "BTC"}, private=True)

    async def get_ticker(self, instrument_name: str) -> Dict[str, Any]:
        return await self._get("public/ticker", {"instrument_name": instrument_name})

    async def buy(self, instrument_name: str, amount: float, price: Optional[float] = None, order_type: str = "limit") -> Dict[str, Any]:
        params = {"instrument_name": instrument_name, "amount": amount, "type": order_type}
        if price:
            params["price"] = price
        return await self._post("private/buy", params)

    async def sell(self, instrument_name: str, amount: float, price: Optional[float] = None, order_type: str = "limit") -> Dict[str, Any]:
        params = {"instrument_name": instrument_name, "amount": amount, "type": order_type}
        if price:
            params["price"] = price
        return await self._post("private/sell", params)

    async def find_instruments_by_delta(self, target_delta: float, target_dte: int = 30, opt_type: str = "C") -> list:
        spot = await self.get_btc_spot_price()
        instruments = await self._get("public/get_instruments", {"currency": "BTC", "kind": "option"})

        matches = []
        for i in instruments:
            name = i["instrument_name"]
            parts = name.split("-")
            if len(parts) != 4 or parts[3] != opt_type:
                continue
            try:
                expiry = datetime.strptime(parts[1], "%d%b%y")
                dte = (expiry - datetime.now(UTC).replace(tzinfo=None)).days
                if abs(dte - target_dte) <= 10:
                    strike = float(parts[2])
                    if opt_type == "C" and strike > spot:
                        matches.append((name, strike))
                    elif opt_type == "P" and strike < spot:
                        matches.append((name, strike))
            except Exception:
                continue

        matches.sort(key=lambda x: abs(x[1] - spot))

        results = []
        for name, strike in matches[:50]:
            ticker = await self._get("public/ticker", {"instrument_name": name})
            delta = ticker.get("greeks", {}).get("delta", 0)
            if delta != 0:
                results.append({
                    "instrument": name,
                    "delta": delta,
                    "bid": ticker.get("best_bid_price", 0),
                    "ask": ticker.get("best_ask_price", 0),
                })

        results.sort(key=lambda x: abs(abs(x["delta"]) - abs(target_delta)))
        return results[:3]

    async def create_combo(self, legs: list) -> str:
        res = await self._post("public/create_combo", {"legs": legs}, private=False)
        return res["instrument_name"]
