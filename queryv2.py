import requests
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import random
import json

logging.basicConfig(
    filename="solana_scanner.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

@dataclass
class Transaction:
    tx_hash: str
    amount: float
    block_height: int
    block_time: int
    sender: Optional[str] = None
    receiver: Optional[str] = None
    fee: Optional[float] = None
    balance_changes: Dict[str, float] = None

class SolanaRPCClient:
    PUBLIC_ENDPOINTS = [
        "https://api.mainnet-beta.solana.com",
        "https://solana-mainnet.g.alchemy.com/v2/demo",
        "https://solana.public-rpc.com",
        "https://solana-api.projectserum.com",
    ]
    
    def __init__(self, rpc_url: Optional[str] = None, min_amount_threshold: float = 0.001):
        self.rpc_url = rpc_url if rpc_url else random.choice(self.PUBLIC_ENDPOINTS)
        self.session = requests.Session()
        self.request_counter = 0
        self.min_amount_threshold = min_amount_threshold
        logger.info(f"Initialized RPC client with endpoint: {self.rpc_url}")
        
        try:
            latest_block = self.get_latest_block()
            logger.info(f"Successfully connected to RPC. Latest block: {latest_block}")
        except Exception as e:
            logger.error(f"Failed to connect to RPC endpoint: {str(e)}")

    def _make_rpc_request(self, method: str, params: List = None) -> Optional[Dict]:
        self.request_counter += 1
        if self.request_counter % 10 == 0:
            time.sleep(0.1)

        data = {
            "jsonrpc": "2.0",
            "id": self.request_counter,
            "method": method,
            "params": params or []
        }

        for attempt in range(3):
            try:
                response = self.session.post(self.rpc_url, json=data, timeout=30)
                if response.status_code == 200:
                    return response.json()
                logger.warning(f"Request failed with status {response.status_code}")
            except Exception as e:
                logger.error(f"RPC request failed (attempt {attempt + 1}): {str(e)}")
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
        return None

    def get_recent_blocks(self, limit: int = 5) -> List[Tuple[int, int]]:
        latest_block = self.get_latest_block()
        logger.info(f"Getting recent blocks. Latest block: {latest_block}")
        
        response = self._make_rpc_request(
            "getBlocks",
            [latest_block - limit, latest_block, {"commitment": "finalized"}]
        )

        if not response or "result" not in response:
            logger.error("Failed to get recent blocks")
            return []

        blocks = []
        for slot in response["result"]:
            block_time = self.get_block_time(slot)
            if block_time:
                blocks.append((slot, block_time))

        return sorted(blocks, reverse=True)

    def get_latest_block(self) -> int:
        response = self._make_rpc_request(
            "getSlot",
            [{"commitment": "finalized"}]
        )
        return response.get("result", 0) if response else 0

    def get_block_time(self, slot: int) -> Optional[int]:
        response = self._make_rpc_request("getBlockTime", [slot])
        return response.get("result") if response else None

    def get_block_transactions(self, slot: int) -> List[Dict]:
        response = self._make_rpc_request(
            "getBlock",
            [
                slot,
                {
                    "encoding": "json",
                    "transactionDetails": "full",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "finalized"
                }
            ]
        )

        if response and "result" in response:
            return response["result"].get("transactions", [])
        return []

class TransactionScanner:
    def __init__(self, rpc_url: str = None, min_amount_threshold: float = 0.001):
        self.client = SolanaRPCClient(rpc_url, min_amount_threshold)
        logger.info(f"Scanner initialized with RPC endpoint: {self.client.rpc_url}")

    def _analyze_balance_changes(self, pre_balances: List[int], post_balances: List[int], 
                               account_keys: List[str]) -> Dict[str, Dict[str, float]]:
        changes = {}
        for i in range(len(pre_balances)):
            if i >= len(post_balances) or i >= len(account_keys):
                continue
                
            pre_bal = pre_balances[i] / 1e9
            post_bal = post_balances[i] / 1e9
            change = post_bal - pre_bal
            
            if abs(change) >= self.client.min_amount_threshold:
                changes[account_keys[i]] = {
                    "pre_balance": pre_bal,
                    "post_balance": post_bal,
                    "change": change
                }
        return changes

    def _parse_transaction(self, tx: Dict, target_amount: float, tolerance: float) -> Optional[Dict]:
        try:
            meta = tx.get("meta", {})
            message = tx["transaction"].get("message", {}) if "transaction" in tx else tx.get("message", {})
            
            if meta.get("err") is not None:
                return None
                    
            pre_balances = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])
            
            if not pre_balances or not post_balances:
                return None
            
            account_keys = (message.get("accountKeys", []) 
                          if isinstance(message.get("accountKeys"), list) 
                          else message.get("accounts", []))
            
            if not account_keys:
                return None
            
            balance_changes = self._analyze_balance_changes(pre_balances, post_balances, account_keys)
            
            significant_transfers = []
            for account, changes in balance_changes.items():
                if abs(changes["change"]) >= self.client.min_amount_threshold:
                    significant_transfers.append({
                        "account": account,
                        "change": changes["change"],
                        "details": changes
                    })
            
            matches = []
            for t in significant_transfers:
                amount = abs(t["change"])
                if abs(amount - target_amount) <= tolerance:
                    matches.append(t)
            
            if matches:
                best_match = max(matches, key=lambda x: abs(x["change"]))
                
                counterparty = None
                for t in significant_transfers:
                    if (t["account"] != best_match["account"] and 
                        abs(t["change"] + best_match["change"]) < self.client.min_amount_threshold):
                        counterparty = t
                        break
                
                result = {
                    "amount": abs(best_match["change"]),
                    "sender": best_match["account"] if best_match["change"] < 0 else counterparty["account"] if counterparty else None,
                    "receiver": best_match["account"] if best_match["change"] > 0 else counterparty["account"] if counterparty else None,
                    "fee": meta.get("fee", 0) / 1e9,
                    "balance_changes": balance_changes
                }
                return result
                                
        except Exception as e:
            logger.error(f"Error parsing transaction: {str(e)}")
        return None

    def scan_for_amount(self, amount_in_sol: float, block_limit: int = 5, tolerance: float = 0.1) -> List[Transaction]:
        matches = []
        logger.info(f"Starting scan for {amount_in_sol} ±{tolerance} SOL transactions")

        blocks = self.client.get_recent_blocks(block_limit)
        if not blocks:
            logger.warning("No blocks retrieved")
            return matches

        total_txs = 0
        for idx, (slot, block_time) in enumerate(blocks, 1):
            logger.info(f"Scanning block {idx}/{len(blocks)} at slot {slot}")
            print(f"\rProgress: {idx}/{len(blocks)} blocks scanned...", end="", flush=True)
            
            transactions = self.client.get_block_transactions(slot)
            total_txs += len(transactions)
            
            for tx_idx, tx in enumerate(transactions):
                transfer = self._parse_transaction(tx, amount_in_sol, tolerance)
                if transfer:
                    matches.append(Transaction(
                        tx_hash=tx.get("transaction", {}).get("signatures", [""])[0],
                        amount=transfer["amount"],
                        block_height=slot,
                        block_time=block_time,
                        sender=transfer["sender"],
                        receiver=transfer["receiver"],
                        fee=transfer["fee"],
                        balance_changes=transfer["balance_changes"]
                    ))

        print("\n")
        logger.info(f"Scan complete. Processed {total_txs} transactions across {len(blocks)} blocks")
        return matches

def main():
    try:
        print("\nSolana Transaction Scanner")
        print("-------------------------")
        
        debug_mode = input("Enable debug mode? (y/n, default: n): ").lower().startswith('y')
        if debug_mode:
            logger.setLevel(logging.DEBUG)
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(levelname)s - %(message)s')
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
            print("Debug mode enabled - detailed output will be shown")
        
        min_amount = float(input("Enter minimum transaction amount to consider in SOL (default 0.001): ") or 0.001)
        if min_amount <= 0:
            raise ValueError("Minimum amount must be positive")
        
        custom_rpc = input("Enter custom RPC URL (or press Enter to use public endpoints): ").strip()
        
        scanner = TransactionScanner(custom_rpc, min_amount)
        print(f"\nConnected to RPC endpoint: {scanner.client.rpc_url}")
        
        amount_in_sol = float(input("Enter the amount in SOL to search for: "))
        if amount_in_sol <= 0:
            raise ValueError("Amount must be positive")

        tolerance = float(input(f"Enter amount tolerance in SOL (default 0.1): ") or 0.1)
        if tolerance < 0:
            raise ValueError("Tolerance must be positive")

        block_limit = int(input("Enter number of recent blocks to scan (default 100): ") or 100)
        
        print(f"\nScanning last {block_limit} blocks for {amount_in_sol} ±{tolerance} SOL transactions...")
        print(f"Filtering out transactions smaller than {min_amount} SOL")
        
        start_time = time.time()
        matches = scanner.scan_for_amount(amount_in_sol, block_limit, tolerance)
        scan_time = time.time() - start_time

        if matches:
            print(f"\nFound {len(matches)} transactions matching {amount_in_sol} ±{tolerance} SOL:")
            for match in matches:
                print("\nTransaction Details:")
                print(f"Hash: {match.tx_hash}")
                print(f"Amount: {match.amount} SOL")
                print(f"Block Height: {match.block_height}")
                print(f"Time: {datetime.fromtimestamp(match.block_time).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                
                if match.sender:
                    print(f"Sender: {match.sender}")
                if match.receiver:
                    print(f"Receiver: {match.receiver}")
                if match.fee is not None:
                    print(f"Transaction Fee: {match.fee:.6f} SOL")
                
                if match.balance_changes:
                    print("\nDetailed Balance Changes:")
                    for account, changes in match.balance_changes.items():
                        print(f"\nAccount: {account}")
                        print(f"  Pre-balance:  {changes['pre_balance']:.9f} SOL")
                        print(f"  Post-balance: {changes['post_balance']:.9f} SOL")
                        print(f"  Net change:   {changes['change']:.9f} SOL")
                
                print("-" * 50)
        else:
            print(f"\nNo transactions found with {amount_in_sol} ±{tolerance} SOL in the last {block_limit} blocks.")
            
        print(f"\nScan completed in {scan_time:.2f} seconds")

    except ValueError as e:
        print(f"Invalid input: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        print("An error occurred. Check the log file for details.")

if __name__ == "__main__":
    main()