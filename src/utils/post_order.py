"""
Post order to Polymarket
"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))); import src.lib_core
from typing import Optional, Dict, Any, List, Tuple
from ..config.env import ENV
from ..models.user_history import get_user_activity_collection
from ..utils.logger import info, warning, order_result
from ..config.copy_strategy import calculate_order_size, get_trade_multiplier

RETRY_LIMIT = ENV.RETRY_LIMIT
COPY_STRATEGY_CONFIG = ENV.COPY_STRATEGY_CONFIG

# Polymarket minimum order sizes
MIN_ORDER_SIZE_USD = 1.0  # Minimum order size in USD for BUY orders
MIN_ORDER_SIZE_TOKENS = 1.0  # Minimum order size in tokens for SELL/MERGE orders


def extract_order_error(response: Any) -> Optional[str]:
    """Extract error message from order response"""
    if not response:
        return None
    
    if isinstance(response, str):
        return response
    
    if isinstance(response, dict):
        # Check direct error
        if 'error' in response:
            error_val = response['error']
            if isinstance(error_val, str):
                return error_val
            if isinstance(error_val, dict):
                if 'error' in error_val:
                    return error_val['error']
                if 'message' in error_val:
                    return error_val['message']
        
        # Check other error fields
        if 'errorMsg' in response:
            return response['errorMsg']
        if 'message' in response:
            return response['message']
    
    return None


def is_insufficient_balance_or_allowance_error(message: Optional[str]) -> bool:
    """Check if error is related to insufficient balance or allowance"""
    if not message:
        return False
    lower = message.lower()
    return 'not enough balance' in lower or 'allowance' in lower


def is_not_found_error(err: Exception) -> bool:
    """Check if exception indicates HTTP 404 from order book endpoint."""
    return '404' in str(err) and 'book?token_id=' in str(err)


async def fetch_json_if_ok(url: str) -> Optional[Any]:
    """Fetch JSON from URL and return None on HTTP/network errors."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            return response.json()
    except Exception:
        return None


def extract_token_ids_from_market_payload(payload: Any) -> List[str]:
    """Extract token ids from a CLOB/Gamma market payload with flexible schema handling."""
    markets: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        data = payload.get('data')
        if isinstance(data, list):
            markets.extend([m for m in data if isinstance(m, dict)])
        elif isinstance(data, dict):
            markets.append(data)
        markets.append(payload)
    elif isinstance(payload, list):
        markets.extend([m for m in payload if isinstance(m, dict)])

    token_ids: List[str] = []
    seen = set()
    for market in markets:
        tokens = market.get('tokens')
        if not isinstance(tokens, list):
            continue

        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_id = token.get('token_id') or token.get('tokenId') or token.get('id')
            if token_id is None:
                continue
            token_id_str = str(token_id).strip()
            if not token_id_str or token_id_str in seen:
                continue
            seen.add(token_id_str)
            token_ids.append(token_id_str)

    return token_ids


async def post_order(
    clob_client: Any,
    condition: str,
    my_position: Optional[Dict[str, Any]],
    user_position: Optional[Dict[str, Any]],
    trade: Dict[str, Any],
    my_balance: float,
    user_balance: float,
    user_address: str
):
    """Post order to Polymarket"""
    collection = get_user_activity_collection(user_address)

    async def discover_execution_asset_candidates() -> List[str]:
        """Discover candidate token ids from CLOB/Gamma market metadata using conditionId."""
        condition_id = trade.get('conditionId')
        if not condition_id:
            return []

        # Try both CLOB and Gamma, with schema/key variations.
        clob_host = getattr(clob_client, 'host', 'https://clob.polymarket.com').rstrip('/')
        condition_id_str = str(condition_id)
        urls = [
            f"{clob_host}/markets?condition_id={condition_id_str}",
            f"{clob_host}/markets?conditionId={condition_id_str}",
            f"https://gamma-api.polymarket.com/markets?conditionId={condition_id_str}",
            f"https://gamma-api.polymarket.com/markets?condition_id={condition_id_str}",
        ]

        discovered: List[str] = []
        seen = set()
        for url in urls:
            payload = await fetch_json_if_ok(url)
            if payload is None:
                continue
            for token_id in extract_token_ids_from_market_payload(payload):
                if token_id in seen:
                    continue
                seen.add(token_id)
                discovered.append(token_id)

        return discovered

    def resolve_execution_asset_candidates(order_condition: str) -> List[str]:
        """Build prioritized candidate token IDs for order book/order creation."""
        # BUY should prefer trader's current position token. MERGE/SELL should prefer our token.
        candidate_values: List[Any] = []
        if order_condition == 'buy':
            candidate_values.extend([
                user_position.get('asset') if user_position else None,
                trade.get('asset'),
                my_position.get('asset') if my_position else None,
            ])
        else:
            candidate_values.extend([
                my_position.get('asset') if my_position else None,
                trade.get('asset'),
                user_position.get('asset') if user_position else None,
            ])

        # Also consider opposite asset if available (can help when RTDS payload points to opposite side).
        if my_position and my_position.get('oppositeAsset'):
            candidate_values.append(my_position.get('oppositeAsset'))
        if user_position and user_position.get('oppositeAsset'):
            candidate_values.append(user_position.get('oppositeAsset'))

        candidates: List[str] = []
        seen = set()
        for value in candidate_values:
            if value is None:
                continue
            token = str(value).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            candidates.append(token)
        return candidates

    async def get_order_book_with_fallback(asset_candidates: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Try candidate token IDs until an order book is found or all candidates fail."""
        if not asset_candidates:
            return None, None

        last_error: Optional[Exception] = None
        for token_id in asset_candidates:
            try:
                order_book = await clob_client.get_order_book(token_id)
                return order_book, token_id
            except Exception as e:
                last_error = e
                if is_not_found_error(e):
                    warning(f'Order book not found for token_id={token_id}; trying next candidate if available')
                    continue
                raise

        if last_error:
            raise last_error
        return None, None
    
    if condition == 'merge':
        info('Executing MERGE strategy...')
        if not my_position:
            warning('No position to merge')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        remaining = my_position.get('size', 0)
        execution_asset_candidates = resolve_execution_asset_candidates('merge')
        discovered_candidates = await discover_execution_asset_candidates()
        execution_asset_candidates.extend([c for c in discovered_candidates if c not in execution_asset_candidates])

        if not execution_asset_candidates:
            warning('Missing token id for merge order - skipping')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        # Check minimum order size
        if remaining < MIN_ORDER_SIZE_TOKENS:
            warning(f'Position size ({remaining:.2f} tokens) too small to merge - skipping')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        retry = 0
        abort_due_to_funds = False
        
        while remaining > 0 and retry < RETRY_LIMIT:
            try:
                order_book, execution_asset = await get_order_book_with_fallback(execution_asset_candidates)
                if not order_book or not execution_asset:
                    warning('No valid token id found for merge order - skipping')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                if not order_book.get('bids') or len(order_book['bids']) == 0:
                    warning('No bids available in order book')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                
                max_price_bid = max(order_book['bids'], key=lambda x: float(x['price']))
                
                info(f'Best bid: {max_price_bid["size"]} @ ${max_price_bid["price"]}')
                
                if remaining <= float(max_price_bid['size']):
                    order_args = {
                        'side': 'SELL',
                        'tokenID': execution_asset,
                        'amount': remaining,
                        'price': float(max_price_bid['price']),
                    }
                else:
                    order_args = {
                        'side': 'SELL',
                        'tokenID': execution_asset,
                        'amount': float(max_price_bid['size']),
                        'price': float(max_price_bid['price']),
                    }
                
                signed_order = await clob_client.create_market_order(order_args)
                resp = await clob_client.post_order(signed_order, 'FOK')
                
                if resp.get('success') is True:
                    retry = 0
                    order_result(True, f'Sold {order_args["amount"]} tokens at ${order_args["price"]}')
                    remaining -= order_args['amount']
                else:
                    error_message = extract_order_error(resp)
                    if is_insufficient_balance_or_allowance_error(error_message):
                        abort_due_to_funds = True
                        warning(f'Order rejected: {error_message or "Insufficient balance or allowance"}')
                        warning('Skipping remaining attempts. Top up funds or check allowance before retrying.')
                        break
                    retry += 1
                    warning(f'Order failed (attempt {retry}/{RETRY_LIMIT}){f" - {error_message}" if error_message else ""}')
            except Exception as e:
                if is_not_found_error(e):
                    warning('No order book found for any candidate token id - skipping this trade')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                retry += 1
                warning(f'Order error (attempt {retry}/{RETRY_LIMIT}): {e}')
        
        if abort_due_to_funds:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': RETRY_LIMIT}}
            )
            return
        
        if retry >= RETRY_LIMIT:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': retry}}
            )
        else:
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
    
    elif condition == 'buy':
        info('Executing BUY strategy...')
        execution_asset_candidates = resolve_execution_asset_candidates('buy')
        discovered_candidates = await discover_execution_asset_candidates()
        execution_asset_candidates.extend([c for c in discovered_candidates if c not in execution_asset_candidates])

        if not execution_asset_candidates:
            warning('Missing token id for buy order - skipping')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        info(f'Your balance: ${my_balance:.2f}')
        info(f'Trader bought: ${trade.get("usdcSize", 0):.2f}')
        
        # Get current position size for position limit checks
        current_position_value = (my_position.get('size', 0) * my_position.get('avgPrice', 0)) if my_position else 0
        
        # Use new copy strategy system
        order_calc = calculate_order_size(
            COPY_STRATEGY_CONFIG,
            trade.get('usdcSize', 0),
            my_balance,
            current_position_value
        )
        
        # Log the calculation reasoning
        info(f'{order_calc.reasoning}')
        
        # Check if order should be executed
        if order_calc.final_amount == 0:
            warning(f'Cannot execute: {order_calc.reasoning}')
            if order_calc.below_minimum:
                warning('Increase COPY_SIZE or wait for larger trades')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        remaining = order_calc.final_amount
        available_balance = my_balance  # Track remaining balance after orders
        
        retry = 0
        abort_due_to_funds = False
        total_bought_tokens = 0  # Track total tokens bought for this trade
        
        while remaining > 0 and retry < RETRY_LIMIT:
            try:
                order_book, execution_asset = await get_order_book_with_fallback(execution_asset_candidates)
                if not order_book or not execution_asset:
                    warning('No valid token id found for buy order - skipping')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                if not order_book.get('asks') or len(order_book['asks']) == 0:
                    warning('No asks available in order book')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                
                min_price_ask = min(order_book['asks'], key=lambda x: float(x['price']))
                
                info(f'Best ask: {min_price_ask["size"]} @ ${min_price_ask["price"]}')
                
                # Check if remaining amount is below minimum before creating order
                if remaining < MIN_ORDER_SIZE_USD:
                    info(f'Remaining amount (${remaining:.2f}) below minimum - completing trade')
                    collection.update_one(
                        {'_id': trade['_id']},
                        {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
                    )
                    break
                
                max_order_size = float(min_price_ask['size']) * float(min_price_ask['price'])
                order_size = min(remaining, max_order_size)
                
                # Ensure minimum order size is 1 USDC
                if order_size < MIN_ORDER_SIZE_USD:
                    info(f'Order size (${order_size:.2f}) below minimum (${MIN_ORDER_SIZE_USD}) - completing trade')
                    collection.update_one(
                        {'_id': trade['_id']},
                        {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
                    )
                    break
                
                # Check if balance is sufficient for the order
                if available_balance < order_size:
                    warning(f'Insufficient balance: Need ${order_size:.2f} but only have ${available_balance:.2f}')
                    abort_due_to_funds = True
                    break
                
                order_args = {
                    'side': 'BUY',
                    'tokenID': execution_asset,
                    'amount': order_size,
                    'price': float(min_price_ask['price']),
                }
                
                info(f'Creating order: ${order_size:.2f} @ ${min_price_ask["price"]} (Balance: ${available_balance:.2f})')
                
                signed_order = await clob_client.create_market_order(order_args)
                resp = await clob_client.post_order(signed_order, 'FOK')
                
                if resp.get('success') is True:
                    retry = 0
                    tokens_bought = order_args['amount'] / order_args['price']
                    total_bought_tokens += tokens_bought
                    order_result(
                        True,
                        f'Bought ${order_args["amount"]:.2f} at ${order_args["price"]} ({tokens_bought:.2f} tokens)'
                    )
                    remaining -= order_args['amount']
                    # Update balance after successful order
                    available_balance -= order_args['amount']
                else:
                    error_message = extract_order_error(resp)
                    if is_insufficient_balance_or_allowance_error(error_message):
                        abort_due_to_funds = True
                        warning(f'Order rejected: {error_message or "Insufficient balance or allowance"}')
                        warning('Skipping remaining attempts. Top up funds or check allowance before retrying.')
                        break
                    retry += 1
                    warning(f'Order failed (attempt {retry}/{RETRY_LIMIT}){f" - {error_message}" if error_message else ""}')
            except Exception as e:
                if is_not_found_error(e):
                    warning('No order book found for any candidate token id - skipping this trade')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                retry += 1
                warning(f'Order error (attempt {retry}/{RETRY_LIMIT}): {e}')
        
        if abort_due_to_funds:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': RETRY_LIMIT}}
            )
            return
        
        if retry >= RETRY_LIMIT:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': retry}}
            )
        else:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
            )
    
    elif condition == 'sell':
        # SELL strategy - similar to merge but different logic
        info('Executing SELL strategy...')
        # Implementation similar to merge but for selling positions
        # This would be implemented based on the full TypeScript version
        collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
