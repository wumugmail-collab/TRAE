#!/usr/bin/env python3
"""BSC 链上只读查询 + 未签名交易组装工具。

设计原则：
- 只读查询不需要任何密钥，直接查公共 RPC。
- 交易只"组装"成未签名负载（unsigned payload），私钥永远不进入本环境；
  由用户在自己的钱包里对交易哈希签名后再广播。
"""
import sys
import json
import requests

RPC = "https://bsc-rpc.publicnode.com"
CHAIN_ID = 56

# BSC 上常见 BEP-20 代币（地址, 符号, 精度）
TOKENS = [
    ("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "WBNB", 18),
    ("0x55d398326f99059fF775485246999027B3197955", "USDT", 18),
    ("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "USDC", 18),
    ("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56", "BUSD", 18),
    ("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", "CAKE", 18),
    ("0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", "BTCB", 18),
    ("0x2170Ed0880ac9A755fd29B2688956BD959F933F8", "ETH", 18),
]

_id = 0


def rpc(method, params=None):
    global _id
    _id += 1
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": _id, "method": method, "params": params or []},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def to_addr_param(addr):
    # calldata 里的 32 字节地址参数：纯 hex，不带 0x 前缀
    return addr.lower().replace("0x", "").rjust(64, "0")


def bnb_balance(addr):
    wei = int(rpc("eth_getBalance", [addr, "latest"]), 16)
    return wei / 1e18


def nonce(addr):
    return int(rpc("eth_getTransactionCount", [addr, "latest"]), 16)


def is_contract(addr):
    code = rpc("eth_getCode", [addr, "latest"])
    return code not in ("0x", "0x0", None)


def token_balance(addr, token):
    # balanceOf(address) selector = 0x70a08231
    data = "0x70a08231" + to_addr_param(addr)
    res = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    if res in ("0x", None):
        return 0
    return int(res, 16)


def snapshot(addr):
    print(f"地址: {addr}")
    print(f"是否合约: {'是' if is_contract(addr) else '否（普通账户）'}")
    print(f"BNB 余额: {bnb_balance(addr):.6f} BNB")
    print(f"Nonce(已发交易数): {nonce(addr)}")
    print("-" * 40)
    print("主要代币余额:")
    for token, sym, dec in TOKENS:
        try:
            raw = token_balance(addr, token)
            val = raw / (10 ** dec)
            if val > 0:
                print(f"  {sym:>6}: {val:,.4f}")
        except Exception as e:  # 单个代币失败不影响整体
            print(f"  {sym:>6}: 查询失败 {e}")
    print("-" * 40)
    print("注：完整交易历史需要区块浏览器索引（BscScan API key），")
    print("    公共 RPC 无法按地址反查历史。如需历史，请提供 BscScan key。")


def build_bnb_transfer(from_addr, to_addr, amount_bnb, gas_price_gwei=1.0):
    """组装一笔 BNB 原生转账的【未签名】负载。不接触私钥。"""
    tx = {
        "chainId": CHAIN_ID,
        "from": from_addr,
        "to": to_addr,
        "value": hex(int(amount_bnb * 1e18)),
        "nonce": hex(nonce(from_addr)),
        "gas": hex(21000),
        "gasPrice": hex(int(gas_price_gwei * 1e9)),
        "data": "0x",
    }
    return tx


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 bsc_query.py <地址>")
        sys.exit(1)
    snapshot(sys.argv[1])
