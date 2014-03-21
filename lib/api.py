import os
import json
import re
import time
import datetime
import base64
import decimal
import operator
import logging
import copy

from logging import handlers as logging_handlers
from gevent import wsgi
import cherrypy
from cherrypy.process import plugins
from jsonrpc import JSONRPCResponseManager, dispatcher
import pymongo
from bson import json_util

from . import (config, util)

PREFERENCES_MAX_LENGTH = 100000 #in bytes, as expressed in JSON
D = decimal.Decimal


def get_block_indexes_for_dates(mongo_db, start_dt, end_dt):
    """Returns a 2 tuple (start_block, end_block) result for the block range that encompasses the given start_date
    and end_date unix timestamps"""
    start_block = mongo_db.processed_blocks.find_one({"block_time": {"$lte": start_dt} }, sort=[("block_time", pymongo.DESCENDING)])
    end_block = mongo_db.processed_blocks.find_one({"block_time": {"$gte": end_dt} }, sort=[("block_time", pymongo.ASCENDING)])
    start_block_index = config.BLOCK_FIRST if not start_block else start_block['block_index']
    if not end_block:
        end_block_index = mongo_db.processed_blocks.find_one(sort=[("block_index", pymongo.DESCENDING)])['block_index']
    else:
        end_block_index = end_block['block_index']
    return (start_block_index, end_block_index)

def get_block_time(mongo_db, block_index):
    """TODO: implement result caching to avoid having to go out to the database"""
    block = mongo_db.processed_blocks.find_one({"block_index": block_index })
    if not block: return None
    return block['block_time']

def serve_api(mongo_db, redis_client):
    # Preferneces are just JSON objects... since we don't force a specific form to the wallet on
    # the server side, this makes it easier for 3rd party wallets (i.e. not counterwallet) to fully be able to
    # use counterwalletd to not only pull useful data, but also load and store their own preferences, containing
    # whatever data they need
    
    DEFAULT_COUNTERPARTYD_API_CACHE_PERIOD = 60 #in seconds
    
    @dispatcher.add_method
    def is_ready():
        """this method used by the client to check if the server is alive, caught up, and ready to accept requests.
        If the server is NOT caught up, a 525 error will be returned actually before hitting this point. Thus,
        if we actually return data from this function, it should always be true. (may change this behaviour later)"""
        assert config.CAUGHT_UP
        return {
            'caught_up': config.CAUGHT_UP,
            'last_message_index': config.LAST_MESSAGE_INDEX, 
            'testnet': config.TESTNET 
        }
    
    @dispatcher.add_method
    def get_reflected_host_info():
        """Allows the requesting host to get some info about itself, such as its IP. Used for troubleshooting."""
        return {
            'ip': cherrypy.request.headers.get('X-Real-Ip', cherrypy.request.headers['Remote-Addr']),
            'cookie': cherrypy.request.headers.get('Cookie', '')
        }
    
    @dispatcher.add_method
    def get_messagefeed_messages_by_index(message_indexes): #yeah, dumb name :)
        messages = util.call_jsonrpc_api("get_messages_by_index", [message_indexes,], abort_on_error=True)['result']
        events = []
        for m in messages:
            events.append(util.create_message_feed_obj_from_cpd_message(mongo_db, m))
        return events

    @dispatcher.add_method
    def get_btc_block_height():
        data = util.call_insight_api('/api/status?q=getInfo', abort_on_error=True)
        return data['info']['blocks']

    @dispatcher.add_method
    def get_btc_address_info(addresses, with_uxtos=True, with_last_txn_hashes=4, with_block_height=False):
        if not isinstance(addresses, list):
            raise Exception("addresses must be a list of addresses, even if it just contains one address")
        results = []
        if with_block_height:
            block_height_response = util.call_insight_api('/api/status?q=getInfo', abort_on_error=True)
            block_height = block_height_response['info']['blocks'] if block_height_response else None
        for address in addresses:
            info = util.call_insight_api('/api/addr/' + address + '/', abort_on_error=True)
            txns = info['transactions']
            del info['transactions']

            result = {}
            result['addr'] = address
            result['info'] = info
            if with_block_height: result['block_height'] = block_height
            #^ yeah, hacky...it will be the same block height for each address (we do this to avoid an extra API call to get_btc_block_height)
            if with_uxtos:
                result['uxtos'] = util.call_insight_api('/api/addr/' + address + '/utxo/', abort_on_error=True)
            if with_last_txn_hashes:
                #with last_txns, only show CONFIRMED txns (so skip the first info['unconfirmedTxApperances'] # of txns, if not 0
                result['last_txns'] = txns[info['unconfirmedTxApperances']:with_last_txn_hashes+info['unconfirmedTxApperances']]
            results.append(result)
        return results

    @dispatcher.add_method
    def get_btc_txns_status(txn_hashes):
        if not isinstance(txn_hashes, list):
            raise Exception("txn_hashes must be a list of txn hashes, even if it just contains one hash")
        results = []
        for tx_hash in txn_hashes:
            tx_info = util.call_insight_api('/api/tx/' + tx_hash + '/', abort_on_error=False)
            if tx_info:
                assert tx_info['txid'] == tx_hash
                results.append({
                    'tx_hash': tx_info['txid'],
                    'blockhash': tx_info.get('blockhash', None), #not provided if not confirmed on network
                    'confirmations': tx_info.get('confirmations', 0), #not provided if not confirmed on network
                    'blocktime': tx_info.get('time', None),
                })
        return results

    @dispatcher.add_method
    def get_normalized_balances(addresses):
        """
        This call augments counterpartyd's get_balances with a normalized_quantity field. It also will include any owned
        assets for an address, even if their balance is zero. 
        NOTE: Does not retrieve BTC balance. Use get_btc_address_info for that.
        """
        if not isinstance(addresses, list):
            raise Exception("addresses must be a list of addresses, even if it just contains one address")
        if not len(addresses):
            raise Exception("Invalid address list supplied")
        
        filters = []
        for address in addresses:
            filters.append({'field': 'address', 'op': '==', 'value': address})
        
        mappings = {}
        result = util.call_jsonrpc_api("get_balances",
            {'filters': filters, 'filterop': 'or'}, abort_on_error=True)['result']
        data = []
        for d in result:
            if not d['quantity']:
                continue #don't include balances with a zero asset value
            asset_info = mongo_db.tracked_assets.find_one({'asset': d['asset']})
            d['normalized_quantity'] = util.normalize_quantity(d['quantity'], asset_info['divisible'])
            mappings[d['address'] + d['asset']] = d
            data.append(d)
        #include any owned assets for each address, even if their balance is zero
        owned_assets = mongo_db.tracked_assets.find( { '$or': [{'owner': a } for a in addresses] }, { '_history': 0, '_id': 0 } )
        for o in owned_assets:
            if (o['owner'] + o['asset']) not in mappings:
                data.append({
                    'address': o['owner'],
                    'asset': o['asset'],
                    'quantity': 0,
                    'normalized_quantity': 0,
                    'owner': True,
                })
            else:
                mappings[o['owner'] + o['asset']]['owner'] = False
        return data

    @dispatcher.add_method
    def get_raw_transactions(address, start_ts=None, end_ts=None, limit=1000):
        """Gets raw transactions for a particular address or set of addresses
        
        @param address: A single address string
        @param start_ts: The starting date & time. Should be a unix epoch object. If passed as None, defaults to 30 days before the end_date
        @param end_ts: The ending date & time. Should be a unix epoch object. If passed as None, defaults to the current date & time
        @param limit: the maximum number of transactions to return; defaults to ten thousand
        @return: Returns the data, ordered from newest txn to oldest. If any limit is applied, it will cut back from the oldest results
        """
        def get_asset_cached(asset, asset_cache):
            if asset in asset_cache:
                return asset_cache[asset]
            asset_data = mongo_db.tracked_assets.find_one({'asset': asset})
            asset_cache[asset] = asset_data
            return asset_data
        
        asset_cache = {} #ghetto cache to speed asset lookups within the scope of a function call
        
        if not end_ts: #default to current datetime
            end_ts = time.mktime(datetime.datetime.utcnow().timetuple())
        if not start_ts: #default to 30 days before the end date
            start_ts = end_ts - (30 * 24 * 60 * 60) 
        start_block_index, end_block_index = get_block_indexes_for_dates(mongo_db,
            datetime.datetime.utcfromtimestamp(start_ts), datetime.datetime.utcfromtimestamp(end_ts))
        
        #make API call to counterpartyd to get all of the data for the specified address
        txns = []
        d = util.call_jsonrpc_api("get_address",
            {'address': address,
             'start_block': start_block_index,
             'end_block': end_block_index}, abort_on_error=True)['result']
        #mash it all together
        for k, v in d.iteritems():
            if k in ['balances', 'debits', 'credits']:
                continue
            if k in ['sends', 'callbacks']: #add asset divisibility info
                for e in v:
                    asset_info = get_asset_cached(e['asset'], asset_cache)
                    e['_divisible'] = asset_info['divisible']
            if k in ['orders',]: #add asset divisibility info for both assets
                for e in v:
                    give_asset_info = get_asset_cached(e['give_asset'], asset_cache)
                    e['_give_divisible'] = give_asset_info['divisible']
                    get_asset_info = get_asset_cached(e['get_asset'], asset_cache)
                    e['_get_divisible'] = get_asset_info['divisible']
            if k in ['order_matches',]: #add asset divisibility info for both assets
                for e in v:
                    forward_asset_info = get_asset_cached(e['forward_asset'], asset_cache)
                    e['_forward_divisible'] = forward_asset_info['divisible']
                    backward_asset_info = get_asset_cached(e['backward_asset'], asset_cache)
                    e['_backward_divisible'] = backward_asset_info['divisible']
            if k in ['bet_expirations', 'order_expirations', 'bet_match_expirations', 'order_match_expirations']:
                for e in v:
                    e['tx_index'] = 0 #add tx_index to all entries (so we can sort on it secondarily), since these lack it
            for e in v:
                e['_entity'] = k
                block_index = e['block_index'] if 'block_index' in e else e['tx1_block_index']
                e['_block_time'] = get_block_time(mongo_db, block_index)
                e['_tx_index'] = e['tx_index'] if 'tx_index' in e else e['tx1_index']  
            txns += v
        txns = util.multikeysort(txns, ['-_block_time', '-_tx_index'])
        #^ won't be a perfect sort since we don't have tx_indexes for cancellations, but better than nothing
        #txns.sort(key=operator.itemgetter('block_index'))
        return txns 

    @dispatcher.add_method
    def get_base_quote_asset(asset1, asset2):
        """Given two arbitrary assets, returns the base asset and the quote asset.
        """
        base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
        base_asset_info = mongo_db.tracked_assets.find_one({'asset': base_asset})
        quote_asset_info = mongo_db.tracked_assets.find_one({'asset': quote_asset})
        pair_name = "%s/%s" % (base_asset, quote_asset)

        if not base_asset_info or not quote_asset_info:
            raise Exception("Invalid asset(s)")

        return {
            'base_asset': base_asset,
            'quote_asset': quote_asset,
            'pair_name': pair_name
        }

    @dispatcher.add_method
    def get_market_price_summary(asset1, asset2, with_last_trades=0):
        result = _get_market_price_summary(asset1, asset2, with_last_trades)
        return result if result is not None else False
        #^ due to current bug in our jsonrpc stack, just return False if None is returned

    def _get_market_price_summary(asset1, asset2, with_last_trades=0):
        """Gets a synthesized trading "market price" for a specified asset pair (if available), as well as additional info.
        If no price is available, False is returned.
        """
        MARKET_PRICE_DERIVE_NUMLAST = 6 #number of last trades over which to derive the market price
        MARKET_PRICE_DERIVE_WEIGHTS = [1, .9, .72, .6, .4, .3] #good first guess...maybe
        assert(len(MARKET_PRICE_DERIVE_WEIGHTS) == MARKET_PRICE_DERIVE_NUMLAST) #sanity check
        
        #look for the last max 6 trades within the past 10 day window
        base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
        base_asset_info = mongo_db.tracked_assets.find_one({'asset': base_asset})
        quote_asset_info = mongo_db.tracked_assets.find_one({'asset': quote_asset})
        
        if not isinstance(with_last_trades, int) or with_last_trades < 0 or with_last_trades > 30:
            raise Exception("Invalid with_last_trades")
        
        if not base_asset_info or not quote_asset_info:
            raise Exception("Invalid asset(s)")
        
        min_trade_time = datetime.datetime.utcnow() - datetime.timedelta(days=10)
        last_trades = mongo_db.trades.find(
            {
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                'block_time': { "$gte": min_trade_time }
            },
            {'_id': 0, 'block_index': 1, 'block_time': 1, 'unit_price': 1, 'base_quantity_normalized': 1, 'quote_quantity_normalized': 1}
        ).sort("block_time", pymongo.DESCENDING).limit(max(MARKET_PRICE_DERIVE_NUMLAST, with_last_trades))
        if not last_trades.count():
            return None #no suitable trade data to form a market price (return None, NOT False here)
        last_trades = list(last_trades)
        last_trades.reverse() #from newest to oldest
        weighted_inputs = []
        for i in xrange(min(len(last_trades), MARKET_PRICE_DERIVE_NUMLAST)):
            weighted_inputs.append([last_trades[i]['unit_price'], MARKET_PRICE_DERIVE_WEIGHTS[i]])
        market_price = util.weighted_average(weighted_inputs)
        result = {
            'market_price': float(D(market_price).quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN)),
            'base_asset': base_asset,
            'quote_asset': quote_asset,
        }
        if with_last_trades:
            #[0]=block_time, [1]=unit_price, [2]=base_quantity_normalized, [3]=quote_quantity_normalized, [4]=block_index
            result['last_trades'] = [[
                t['block_time'],
                t['unit_price'],
                t['base_quantity_normalized'],
                t['quote_quantity_normalized'],
                t['block_index']
            ] for t in last_trades]
        return result
        
    @dispatcher.add_method
    def get_market_info(assets):
        """Returns information related to capitalization, volume, etc for the supplied asset(s)
        
        NOTE: in_btc == base asset is BTC, in_xcp == base asset is XCP
        
        @param assets: A list of one or more assets
        """
        def calc_inverse(quantity):
            return float( (D(1) / D(quantity) ).quantize(
                D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))            

        def calc_price_change(open, close):
            return float((D(100) * (D(close) - D(open)) / D(open)).quantize(
                    D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))            
        
        asset_data = {}
        start_dt_1d = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        start_dt_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        mps_xcp_btc = _get_market_price_summary('XCP', 'BTC', with_last_trades=30)
        xcp_btc_price = mps_xcp_btc['market_price'] if mps_xcp_btc else None # == XCP/BTC
        btc_xcp_price = calc_inverse(mps_xcp_btc['market_price']) if mps_xcp_btc else None #BTC/XCP
        for asset in assets:
            asset_info = mongo_db.tracked_assets.find_one({'asset': asset})
            #modify some of the properties of the returned asset_info for BTC and XCP
            if asset == 'BTC':
                asset_info['total_issued'] = util.get_btc_supply(normalize=False)
                asset_info['total_issued_normalized'] = util.normalize_quantity(asset_info['total_issued'])
            elif asset == 'XCP':
                asset_info['total_issued'] = util.call_jsonrpc_api("get_xcp_supply", [], abort_on_error=True)['result']
                asset_info['total_issued_normalized'] = util.normalize_quantity(asset_info['total_issued'])
                
            if not asset_info:
                raise Exception("Invalid asset: %s" % asset)
            
            if asset not in ['BTC', 'XCP']:
                #get price data for both the asset with XCP, as well as BTC
                price_summary_in_xcp = _get_market_price_summary(asset, 'XCP', with_last_trades=30)
                price_summary_in_btc = _get_market_price_summary(asset, 'BTC', with_last_trades=30)

                #aggregated (averaged) price (expressed as XCP) for the asset on both the XCP and BTC markets
                if price_summary_in_xcp: # no trade data
                    price_in_xcp = price_summary_in_xcp['market_price']
                    if xcp_btc_price:
                        aggregated_price_in_xcp = float(((D(price_summary_in_xcp['market_price']) + D(xcp_btc_price)) / D(2)).quantize(
                            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                    else: aggregated_price_in_xcp = None
                else:
                    price_in_xcp = None
                    aggregated_price_in_xcp = None
                    
                if price_summary_in_btc: # no trade data
                    price_in_btc = price_summary_in_btc['market_price']
                    if btc_xcp_price:
                        aggregated_price_in_btc = float(((D(price_summary_in_btc['market_price']) + D(btc_xcp_price)) / D(2)).quantize(
                            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                    else: aggregated_price_in_btc = None
                else:
                    aggregated_price_in_btc = None
                    price_in_btc = None
            else:
                #here we take the normal XCP/BTC pair, and invert it to BTC/XCP, to get XCP's data in terms of a BTC base
                # (this is the only area we do this, as BTC/XCP is NOT standard pair ordering)
                price_summary_in_xcp = mps_xcp_btc #might be None
                price_summary_in_btc = copy.deepcopy(mps_xcp_btc) if mps_xcp_btc else None #must invert this -- might be None
                if price_summary_in_btc:
                    price_summary_in_btc['market_price'] = calc_inverse(price_summary_in_btc['market_price'])
                    price_summary_in_btc['base_asset'] = 'BTC'
                    price_summary_in_btc['quote_asset'] = 'XCP'
                    for i in xrange(len(price_summary_in_btc['last_trades'])):
                        #[0]=block_time, [1]=unit_price, [2]=base_quantity_normalized, [3]=quote_quantity_normalized, [4]=block_index
                        price_summary_in_btc['last_trades'][i][1] = calc_inverse(price_summary_in_btc['last_trades'][i][1])
                        price_summary_in_btc['last_trades'][i][2], price_summary_in_btc['last_trades'][i][3] = \
                            price_summary_in_btc['last_trades'][i][3], price_summary_in_btc['last_trades'][i][2] #swap
                if asset == 'XCP':
                    price_in_xcp = 1.0
                    price_in_btc = price_summary_in_btc['market_price'] if price_summary_in_btc else None
                    aggregated_price_in_xcp = 1.0
                    aggregated_price_in_btc = btc_xcp_price #might be None
                else:
                    assert asset == 'BTC'
                    price_in_xcp = price_summary_in_xcp['market_price'] if price_summary_in_xcp else None
                    price_in_btc = 1.0
                    aggregated_price_in_xcp = xcp_btc_price #might be None
                    aggregated_price_in_btc = 1.0
            
            #get XCP and BTC market summarized trades over a 7d period (quantize to hour long slots)
            _7d_history_in_xcp = None # xcp/asset market (or xcp/btc for xcp or btc)
            _7d_history_in_btc = None # btc/asset market (or btc/xcp for xcp or btc)
            if asset not in ['BTC', 'XCP']:
                for a in ['XCP', 'BTC']:
                    _7d_history = mongo_db.trades.aggregate([
                        {"$match": {
                            "base_asset": a,
                            "quote_asset": asset,
                            "block_time": {"$gte": start_dt_7d }
                        }},
                        {"$project": {
                            "year":  {"$year": "$block_time"},
                            "month": {"$month": "$block_time"},
                            "day":   {"$dayOfMonth": "$block_time"},
                            "hour":  {"$hour": "$block_time"},
                            "unit_price": 1,
                            "base_quantity_normalized": 1 #to derive volume
                        }},
                        {"$sort": {"block_time": pymongo.ASCENDING}},
                        {"$group": {
                            "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                            "price": {"$avg": "$unit_price"},
                            "vol":   {"$sum": "$base_quantity_normalized"},
                        }},
                    ])
                    _7d_history = [] if not _7d_history['ok'] else _7d_history['result']
                    if a == 'XCP': _7d_history_in_xcp = _7d_history
                    else: _7d_history_in_btc = _7d_history
            else: #get the XCP/BTC market and invert for BTC/XCP (_7d_history_in_btc)
                _7d_history = mongo_db.trades.aggregate([
                    {"$match": {
                        "base_asset": 'XCP',
                        "quote_asset": 'BTC',
                        "block_time": {"$gte": start_dt_7d }
                    }},
                    {"$project": {
                        "year":  {"$year": "$block_time"},
                        "month": {"$month": "$block_time"},
                        "day":   {"$dayOfMonth": "$block_time"},
                        "hour":  {"$hour": "$block_time"},
                        "unit_price": 1,
                        "base_quantity_normalized": 1 #to derive volume
                    }},
                    {"$sort": {"block_time": pymongo.ASCENDING}},
                    {"$group": {
                        "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                        "price": {"$avg": "$unit_price"},
                        "vol":   {"$sum": "$base_quantity_normalized"},
                    }},
                ])
                _7d_history = [] if not _7d_history['ok'] else _7d_history['result']
                _7d_history_in_xcp = _7d_history
                _7d_history_in_btc = copy.deepcopy(_7d_history_in_xcp)
                for i in xrange(len(_7d_history_in_btc)):
                    _7d_history_in_btc[i]['price'] = calc_inverse(_7d_history_in_btc[i]['price'])
                    _7d_history_in_btc[i]['vol'] = calc_inverse(_7d_history_in_btc[i]['vol'])
            
            for l in [_7d_history_in_xcp, _7d_history_in_btc]:
                for e in l: #convert our _id field out to be an epoch ts (in ms), and delete _id
                    e['when'] = time.mktime(datetime.datetime(e['_id']['year'], e['_id']['month'], e['_id']['day'], e['_id']['hour']).timetuple()) * 1000 
                    del e['_id']

            #perform aggregation to get 24h statistics
            #TOTAL volume and count across all trades for the asset
            _24h_vols = {'vol': 0, 'count': 0}
            _24h_vols_as_base = mongo_db.trades.aggregate([
                {"$match": {
                    "base_asset": asset,
                    "block_time": {"$gte": start_dt_1d } }},
                {"$project": {
                    "base_quantity_normalized": 1 #to derive volume
                }},
                {"$group": {
                    "_id":   1,
                    "vol":   {"$sum": "$base_quantity_normalized"},
                    "count": {"$sum": 1},
                }}
            ])
            _24h_vols_as_base = {} if not _24h_vols_as_base['ok'] \
                or not len(_24h_vols_as_base['result']) else _24h_vols_as_base['result'][0]
            _24h_vols_as_quote = mongo_db.trades.aggregate([
                {"$match": {
                    "quote_asset": asset,
                    "block_time": {"$gte": start_dt_1d } }},
                {"$project": {
                    "quote_quantity_normalized": 1 #to derive volume
                }},
                {"$group": {
                    "_id":   1,
                    "vol":   {"$sum": "quote_quantity_normalized"},
                    "count": {"$sum": 1},
                }}
            ])
            _24h_vols_as_quote = {} if not _24h_vols_as_quote['ok'] \
                or not len(_24h_vols_as_quote['result']) else _24h_vols_as_quote['result'][0]
            _24h_vols['vol'] = _24h_vols_as_base.get('vol', 0) + _24h_vols_as_quote.get('vol', 0) 
            _24h_vols['count'] = _24h_vols_as_base.get('count', 0) + _24h_vols_as_quote.get('count', 0) 
            
            #XCP market volume with stats
            if asset != 'XCP' and price_summary_in_xcp is not None and len(price_summary_in_xcp['last_trades']):
                _24h_ohlc_in_xcp = mongo_db.trades.aggregate([
                    {"$match": {
                        "base_asset": "XCP",
                        "quote_asset": asset,
                        "block_time": {"$gte": start_dt_1d } }},
                    {"$project": {
                        "unit_price": 1,
                        "base_quantity_normalized": 1 #to derive volume
                    }},
                    {"$group": {
                        "_id":   1,
                        "open":  {"$first": "$unit_price"},
                        "high":  {"$max": "$unit_price"},
                        "low":   {"$min": "$unit_price"},
                        "close": {"$last": "$unit_price"},
                        "vol":   {"$sum": "$base_quantity_normalized"},
                        "count": {"$sum": 1},
                    }}
                ])
                _24h_ohlc_in_xcp = {} if not _24h_ohlc_in_xcp['ok'] \
                    or not len(_24h_ohlc_in_xcp['result']) else _24h_ohlc_in_xcp['result'][0]
                if _24h_ohlc_in_xcp: del _24h_ohlc_in_xcp['_id']
            else:
                _24h_ohlc_in_xcp = {}
                
            #BTC market volume with stats
            if asset != 'BTC' and price_summary_in_btc and len(price_summary_in_btc['last_trades']):
                _24h_ohlc_in_btc = mongo_db.trades.aggregate([
                    {"$match": {
                        "base_asset": "BTC",
                        "quote_asset": asset,
                        "block_time": {"$gte": start_dt_1d } }},
                    {"$project": {
                        "unit_price": 1,
                        "base_quantity_normalized": 1 #to derive volume
                    }},
                    {"$group": {
                        "_id":   1,
                        "open":  {"$first": "$unit_price"},
                        "high":  {"$max": "$unit_price"},
                        "low":   {"$min": "$unit_price"},
                        "close": {"$last": "$unit_price"},
                        "vol":   {"$sum": "$base_quantity_normalized"},
                        "count": {"$sum": 1},
                    }}
                ])
                _24h_ohlc_in_btc = {} if not _24h_ohlc_in_btc['ok'] \
                    or not len(_24h_ohlc_in_btc['result']) else _24h_ohlc_in_btc['result'][0]
                if _24h_ohlc_in_btc: del _24h_ohlc_in_btc['_id']
            else:
                _24h_ohlc_in_btc = {}
            
            asset_data[asset] = {
                'price_in_xcp': price_in_xcp, #current price of asset vs XCP (e.g. how many units of asset for 1 unit XCP)
                'price_in_btc': price_in_btc, #current price of asset vs BTC (e.g. how many units of asset for 1 unit BTC)
                'price_as_xcp': calc_inverse(price_in_xcp) if price_in_xcp else None, #current price of asset AS XCP
                'price_as_btc': calc_inverse(price_in_btc) if price_in_btc else None, #current price of asset AS BTC
                'aggregated_price_in_xcp': aggregated_price_in_xcp, 
                'aggregated_price_in_btc': aggregated_price_in_btc,
                'aggregated_price_as_xcp': calc_inverse(aggregated_price_in_xcp) if aggregated_price_in_xcp else None, 
                'aggregated_price_as_btc': calc_inverse(aggregated_price_in_btc) if aggregated_price_in_btc else None,
                'total_supply': asset_info['total_issued_normalized'], 
                'market_cap_in_xcp': float( (D(asset_info['total_issued_normalized']) / D(price_in_xcp)).quantize(
                    D('.00000000'), rounding=decimal.ROUND_HALF_EVEN) ) if price_in_xcp else None,
                'market_cap_in_btc': float( (D(asset_info['total_issued_normalized']) / D(price_in_btc)).quantize(
                    D('.00000000'), rounding=decimal.ROUND_HALF_EVEN) ) if price_in_btc else None,
                '24h_summary': _24h_vols,
                #^ total quantity traded of that asset in all markets in last 24h
                '24h_ohlc_in_xcp': _24h_ohlc_in_xcp,
                #^ quantity of asset traded with BTC in last 24h
                '24h_ohlc_in_btc': _24h_ohlc_in_btc,
                #^ quantity of asset traded with XCP in last 24h
                '24h_vol_price_change_in_xcp': calc_price_change(_24h_ohlc_in_xcp['open'], _24h_ohlc_in_xcp['close'])
                    if _24h_ohlc_in_xcp else None,
                #^ aggregated price change from 24h ago to now, expressed as a signed float (e.g. .54 is +54%, -1.12 is -112%)
                '24h_vol_price_change_in_btc': calc_price_change(_24h_ohlc_in_btc['open'], _24h_ohlc_in_btc['close'])
                    if _24h_ohlc_in_btc else None,
                '7d_history_in_xcp': [[e['when'], e['price']] for e in _7d_history_in_xcp],
                '7d_history_in_btc': [[e['when'], e['price']] for e in _7d_history_in_btc],
            }
        return asset_data

    @dispatcher.add_method
    def get_market_price_history(asset1, asset2, start_ts=None, end_ts=None, as_dict=False):
        """Return block-by-block aggregated market history data for the specified asset pair, within the specified date range.
        @returns List of lists (or list of dicts, if as_dict is specified).
            * If as_dict is False, each embedded list has 8 elements [block time (epoch in MS), open, high, low, close, volume, # trades in block, block index]
            * If as_dict is True, each dict in the list has the keys: block_time (epoch in MS), block_index, open, high, low, close, vol, count 
        """
        if not end_ts: #default to current datetime
            end_ts = time.mktime(datetime.datetime.utcnow().timetuple())
        if not start_ts: #default to 30 days before the end date
            start_ts = end_ts - (30 * 24 * 60 * 60) 
        base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
        
        #get ticks -- open, high, low, close, volume
        result = mongo_db.trades.aggregate([
            {"$match": {
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "block_time": {
                    "$gte": datetime.datetime.utcfromtimestamp(start_ts),
                    "$lte": datetime.datetime.utcfromtimestamp(end_ts)
                }
            }},
            {"$project": {
                "block_time": 1,
                "block_index": 1,
                "unit_price": 1,
                "base_quantity_normalized": 1 #to derive volume
            }},
            {"$group": {
                "_id":   {"block_time": "$block_time", "block_index": "$block_index"},
                "open":  {"$first": "$unit_price"},
                "high":  {"$max": "$unit_price"},
                "low":   {"$min": "$unit_price"},
                "close": {"$last": "$unit_price"},
                "vol":   {"$sum": "$base_quantity_normalized"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id.block_time": pymongo.ASCENDING}},
        ])
        if not result['ok']:
            return False
        if as_dict:
            result = result['result']
            for r in result:
                r['block_time'] = r['_id']['block_time']
                r['block_index'] = r['_id']['block_index']
                del r['_id']
        else:
            result = [
                [r['_id']['block_time'],
                 r['open'], r['high'], r['low'], r['close'], r['vol'],
                 r['count'], r['_id']['block_index']] for r in result['result']
            ]
        return result
    
    @dispatcher.add_method
    def get_trade_history(asset1, asset2, last_trades=50):
        """Gets last N of trades for a specified asset pair"""
        base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
        if last_trades > 500:
            raise Exception("Requesting history of too many trades")
        
        last_trades = mongo_db.trades.find({
            "base_asset": base_asset,
            "quote_asset": quote_asset}, {'_id': 0}).sort("block_time", pymongo.DESCENDING).limit(last_trades)
        if not last_trades.count():
            return False #no suitable trade data to form a market price
        last_trades = list(last_trades)
        return last_trades 
    
    @dispatcher.add_method
    def get_trade_history_within_dates(asset1, asset2, start_ts=None, end_ts=None, limit=50):
        """Gets trades for a certain asset pair between a certain date range, with the max results limited"""
        base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
        if not end_ts: #default to current datetime
            end_ts = time.mktime(datetime.datetime.utcnow().timetuple())
        if not start_ts: #default to 30 days before the end date
            start_ts = end_ts - (30 * 24 * 60 * 60) 

        if limit > 500:
            raise Exception("Requesting history of too many trades")
        
        last_trades = mongo_db.trades.find({
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "block_time": {
                    "$gte": datetime.datetime.utcfromtimestamp(start_ts),
                    "$lte": datetime.datetime.utcfromtimestamp(end_ts)
                  }
            }, {'_id': 0}).sort("block_time", pymongo.DESCENDING).limit(limit)
        if not last_trades.count():
            return False #no suitable trade data to form a market price
        last_trades = list(last_trades)
        return last_trades 

    @dispatcher.add_method
    def get_order_book(buy_asset, sell_asset, normalized_fee_provided=None, normalized_fee_required=None):
        """Gets the current order book for a specified asset pair
        
        @param: normalized_fee_required: Only specify if buying BTC. If specified, the order book will be pruned down to only
         show orders at and above this fee_required
        @param: normalized_fee_provided: Only specify if selling BTC. If specified, the order book will be pruned down to only
         show orders at and above this fee_provided
        """
        base_asset, quote_asset = util.assets_to_asset_pair(buy_asset, sell_asset)
        base_asset_info = mongo_db.tracked_assets.find_one({'asset': base_asset})
        quote_asset_info = mongo_db.tracked_assets.find_one({'asset': quote_asset})
        
        if not base_asset_info or not quote_asset_info:
            raise Exception("Invalid asset(s)")

        open_sell_orders_filters = [
            {"field": "get_asset", "op": "==", "value": sell_asset},
            {"field": "give_asset", "op": "==", "value": buy_asset},
            {'field': 'give_remaining', 'op': '!=', 'value': 0}, #don't show empty
        ]
        open_sell_orders = util.call_jsonrpc_api("get_orders", {
            'filters': open_sell_orders_filters,
            'show_expired': False,
             'order_by': 'block_index',
             'order_dir': 'asc',
            }, abort_on_error=True)['result']
        
        #TODO: limit # results to 8 or so for each book (we have to sort as well to limit)
        base_bid_filters = [
            {"field": "get_asset", "op": "==", "value": base_asset},
            {"field": "give_asset", "op": "==", "value": quote_asset},
            {'field': 'give_remaining', 'op': '!=', 'value': 0}, #don't show empty
        ]
        base_ask_filters = [
            {"field": "get_asset", "op": "==", "value": quote_asset},
            {"field": "give_asset", "op": "==", "value": base_asset},
            {'field': 'give_remaining', 'op': '!=', 'value': 0}, #don't show empty
        ]
        if base_asset == 'BTC':
            if buy_asset == 'BTC':
                #if BTC is base asset and we're buying it, we're buying the BASE. we require a BTC fee (we're on the bid (bottom) book and we want a lower price)
                # - show BASE buyers (bid book) that require a BTC fee >= what we require (our side of the book)
                # - show BASE sellers (ask book) that provide a BTC fee >= what we require 
                base_bid_filters.append({"field": "fee_required", "op": ">=", "value": util.denormalize_quantity(normalized_fee_required)}) #my competition at the given fee require
                base_ask_filters.append({"field": "fee_provided", "op": ">=", "value": util.denormalize_quantity(normalized_fee_required)})
            elif sell_asset == 'BTC':
                #if BTC is base asset and we're selling it, we're selling the BASE. we provide a BTC fee (we're on the ask (top) book and we want a higher price)
                # - show BASE buyers (bid book) that provide a BTC fee >= what we provide 
                # - show BASE sellers (ask book) that require a BTC fee <= what we provide (our side of the book) 
                base_bid_filters.append({"field": "fee_required", "op": "<=", "value": util.denormalize_quantity(normalized_fee_provided)}) 
                base_ask_filters.append({"field": "fee_provided", "op": ">=", "value": util.denormalize_quantity(normalized_fee_provided)}) #my competition at the given fee provided
        elif quote_asset == 'BTC':
            if buy_asset == 'BTC':
                #if BTC is quote asset and we're buying it, we're selling the BASE. we require a BTC fee (we're on the ask (top) book and we want a higher price)
                # - show BASE buyers (bid book) that provide a BTC fee >= what we require 
                # - show BASE sellers (ask book) that require a BTC fee >= what we require (our side of the book)
                base_bid_filters.append({"field": "fee_provided", "op": ">=", "value": util.denormalize_quantity(normalized_fee_required)})
                base_ask_filters.append({"field": "fee_required", "op": ">=", "value": util.denormalize_quantity(normalized_fee_required)}) #my competitions at the given fee required
            elif sell_asset == 'BTC':
                #if BTC is quote asset and we're selling it, we're buying the BASE. we provide a BTC fee (we're on the bid (bottom) book and we want a lower price)
                # - show BASE buyers (bid book) that provide a BTC fee >= what we provide (our side of the book)
                # - show BASE sellers (ask book) that require a BTC fee <= what we provide 
                base_bid_filters.append({"field": "fee_provided", "op": ">=", "value": util.denormalize_quantity(normalized_fee_provided)}) #my compeitition at the given fee provided
                base_ask_filters.append({"field": "fee_required", "op": "<=", "value": util.denormalize_quantity(normalized_fee_provided)})
            
        base_bid_orders = util.call_jsonrpc_api("get_orders", {
            'filters': base_bid_filters,
            'show_expired': False,
             'order_by': 'block_index',
             'order_dir': 'asc',
            }, abort_on_error=True)['result']

        base_ask_orders = util.call_jsonrpc_api("get_orders", {
            'filters': base_ask_filters,
            'show_expired': False,
             'order_by': 'block_index',
             'order_dir': 'asc',
            }, abort_on_error=True)['result']
        
        def make_book(orders, isBidBook):
            book = {}
            for o in orders:
                if o['give_asset'] == base_asset:
                    give_quantity = util.normalize_quantity(o['give_quantity'], base_asset_info['divisible'])
                    get_quantity = util.normalize_quantity(o['get_quantity'], quote_asset_info['divisible'])
                    unit_price = float(( D(o['get_quantity']) / D(o['give_quantity']) ).quantize(
                        D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                    remaining = util.normalize_quantity(o['give_remaining'], base_asset_info['divisible'])
                else:
                    give_quantity = util.normalize_quantity(o['give_quantity'], quote_asset_info['divisible'])
                    get_quantity = util.normalize_quantity(o['get_quantity'], base_asset_info['divisible'])
                    unit_price = float(( D(o['give_quantity']) / D(o['get_quantity']) ).quantize(
                        D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                    remaining = util.normalize_quantity(o['get_remaining'], base_asset_info['divisible'])
                id = "%s_%s_%s" % (base_asset, quote_asset, unit_price)
                #^ key = {base}_{bid}_{unit_price}, values ref entries in book
                book.setdefault(id, {'unit_price': unit_price, 'quantity': 0, 'count': 0})
                book[id]['quantity'] += remaining #base quantity outstanding
                book[id]['count'] += 1 #num orders at this price level
            book = sorted(book.itervalues(), key=operator.itemgetter('unit_price'), reverse=isBidBook)
            #^ sort -- bid book = descending, ask book = ascending
            return book
        
        #compile into a single book, at volume tiers
        base_bid_book = make_book(base_bid_orders, True)
        base_ask_book = make_book(base_ask_orders, False)
        #get stats like the spread and median
        if base_bid_book and base_ask_book:
            bid_ask_spread = float(( D(base_ask_book[0]['unit_price']) - D(base_bid_book[0]['unit_price']) ).quantize(
                            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        else: bid_ask_spread = 0
        if base_ask_book:
            bid_ask_median = float(( D(base_ask_book[0]['unit_price']) - (D(bid_ask_spread) / 2) ).quantize(
                            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        else: bid_ask_median = 0
        
        #compose depth
        bid_depth = D(0)
        for o in base_bid_book:
            bid_depth += D(o['quantity'])
            o['depth'] = float(bid_depth.quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        bid_depth = float(bid_depth.quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        ask_depth = D(0)
        for o in base_ask_book:
            ask_depth += D(o['quantity'])
            o['depth'] = float(ask_depth.quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        ask_depth = float(ask_depth.quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
        
        #compose raw orders
        orders = base_bid_orders + base_ask_orders
        for o in orders:
            #add in the blocktime to help makes interfaces more user-friendly (i.e. avoid displaying block
            # indexes and display datetimes instead)
            o['block_time'] = time.mktime(get_block_time(mongo_db, o['block_index']).timetuple()) * 1000
        for o in open_sell_orders:
            o['block_time'] = time.mktime(get_block_time(mongo_db, o['block_index']).timetuple()) * 1000
            
        result = {
            'base_bid_book': base_bid_book,
            'base_ask_book': base_ask_book,
            'bid_depth': bid_depth,
            'ask_depth': ask_depth,
            'bid_ask_spread': bid_ask_spread,
            'bid_ask_median': bid_ask_median,
            'raw_orders': orders,
            'open_sell_orders': open_sell_orders
        }
        return result
    
    @dispatcher.add_method
    def get_owned_assets(addresses):
        """Gets a list of owned assets for one or more addresses"""
        result = mongo_db.tracked_assets.find({
            'owner': {"$in": addresses}
        }, {"_id":0}).sort("asset", pymongo.ASCENDING)
        return list(result)


    @dispatcher.add_method
    def get_asset_history(asset, reverse=False):
        """
        Returns a list of changes for the specified asset, from its inception to the current time.
        
        @param asset: The asset to retrieve a history on
        @param reverse: By default, the history is returned in the order of oldest to newest. Set this parameter to True
        to return items in the order of newest to oldest.
        
        @return:
        Changes are returned as a list of dicts, with each dict having the following format:
        * type: One of 'created', 'issued_more', 'changed_description', 'locked', 'transferred', 'called_back'
        * 'at_block': The block number this change took effect
        * 'at_block_time': The block time this change took effect
        
        * IF type = 'created': Has the following fields, as specified when the asset was initially created:
          * owner, description, divisible, locked, total_issued, total_issued_normalized
        * IF type = 'issued_more':
          * 'additional': The additional quantity issued (raw)
          * 'additional_normalized': The additional quantity issued (normalized)
          * 'total_issued': The total issuance after this change (raw)
          * 'total_issued_normalized': The total issuance after this change (normalized)
        * IF type = 'changed_description':
          * 'prev_description': The old description
          * 'new_description': The new description
        * IF type = 'locked': NO EXTRA FIELDS
        * IF type = 'transferred':
          * 'prev_owner': The address the asset was transferred from
          * 'new_owner': The address the asset was transferred to
        * IF type = 'called_back':
          * 'percentage': The percentage of the asset called back (between 0 and 100)
        """
        asset = mongo_db.tracked_assets.find_one({ 'asset': asset }, {"_id":0})
        if not asset:
            raise Exception("Unrecognized asset")
        
        #run down through _history and compose a diff log
        history = []
        raw = asset['_history'] + [asset,] #oldest to newest. add on the current state
        prev = None
        for i in xrange(len(raw)): #oldest to newest
            if i == 0:
                assert raw[i]['_change_type'] == 'created'
                history.append({
                    'type': 'created',
                    'owner': raw[i]['owner'],
                    'description': raw[i]['description'],
                    'divisible': raw[i]['divisible'],
                    'locked': raw[i]['locked'],
                    'total_issued': raw[i]['total_issued'],
                    'total_issued_normalized': raw[i]['total_issued_normalized'],
                    'at_block': raw[i]['_at_block'],
                    'at_block_time': time.mktime(raw[i]['_at_block_time'].timetuple()) * 1000,
                })
                prev = raw[i]
                continue
            
            assert prev
            if raw[i]['_change_type'] == 'locked':
                assert prev['locked'] != raw[i]['locked']
                history.append({
                    'type': 'locked',
                    'at_block': raw[i]['_at_block'],
                    'at_block_time': time.mktime(raw[i]['_at_block_time'].timetuple()) * 1000,
                })
            elif raw[i]['_change_type'] == 'transferred':
                assert prev['owner'] != raw[i]['owner']
                history.append({
                    'type': 'transferred',
                    'at_block': raw[i]['_at_block'],
                    'at_block_time': time.mktime(raw[i]['_at_block_time'].timetuple()) * 1000,
                    'prev_owner': prev['owner'],
                    'new_owner': raw[i]['owner'],
                })
            elif raw[i]['_change_type'] == 'changed_description':
                assert prev['description'] !=  raw[i]['description']
                history.append({
                    'type': 'changed_description',
                    'at_block': raw[i]['_at_block'],
                    'at_block_time': time.mktime(raw[i]['_at_block_time'].timetuple()) * 1000,
                    'prev_description': prev['description'],
                    'new_description': raw[i]['description'],
                })
            else: #issue additional
                assert raw[i]['total_issued'] - prev['total_issued'] > 0
                history.append({
                    'type': 'issued_more',
                    'at_block': raw[i]['_at_block'],
                    'at_block_time': time.mktime(raw[i]['_at_block_time'].timetuple()) * 1000,
                    'additional': raw[i]['total_issued'] - prev['total_issued'],
                    'additional_normalized': raw[i]['total_issued_normalized'] - prev['total_issued_normalized'],
                    'total_issued': raw[i]['total_issued'],
                    'total_issued_normalized': raw[i]['total_issued_normalized'],
                })
            prev = raw[i]
        
        #get callbacks externally via the cpd API, and merge in with the asset history we composed
        callbacks = util.call_jsonrpc_api("get_callbacks",
            [{'field': 'asset', 'op': '==', 'value': asset['asset']},], abort_on_error=True)['result']
        final_history = []
        if len(callbacks):
            for e in history: #history goes from earliest to latest
                if callbacks[0]['block_index'] < e['at_block']: #throw the callback entry in before this one
                    block_time = get_block_time(mongo_db, callbacks[0]['block_index'])
                    assert block_time
                    final_history.append({
                        'type': 'called_back',
                        'at_block': callbacks[0]['block_index'],
                        'at_block_time': time.mktime(block_time.timetuple()) * 1000,
                        'percentage': callbacks[0]['fraction'] * 100,
                    })
                    callbacks.pop(0)
                else:
                    final_history.append(e)
        else:
            final_history = history
        if reverse: final_history.reverse()
        return final_history

    @dispatcher.add_method
    def get_balance_history(asset, addresses, normalize=True, start_ts=None, end_ts=None):
        """Retrieves the ordered balance history for a given address (or list of addresses) and asset pair, within the specified date range
        @param normalize: If set to True, return quantities that (if the asset is divisible) have been divided by 100M (satoshi). 
        @return: A list of tuples, with the first entry of each tuple being the block time (epoch TS), and the second being the new balance
         at that block time.
        """
        if not isinstance(addresses, list):
            raise Exception("addresses must be a list of addresses, even if it just contains one address")
            
        asset_info = mongo_db.tracked_assets.find_one({'asset': asset})
        if not asset_info:
            raise Exception("Asset does not exist.")
            
        if not end_ts: #default to current datetime
            end_ts = time.mktime(datetime.datetime.utcnow().timetuple())
        if not start_ts: #default to 30 days before the end date
            start_ts = end_ts - (30 * 24 * 60 * 60)
        results = []
        for address in addresses:
            result = mongo_db.balance_changes.find({
                'address': address,
                'asset': asset,
                "block_time": {
                    "$gte": datetime.datetime.utcfromtimestamp(start_ts),
                    "$lte": datetime.datetime.utcfromtimestamp(end_ts)
                }
            }).sort("block_time", pymongo.ASCENDING)
            results.append({
                'name': address,
                'data': [
                    (time.mktime(r['block_time'].timetuple()) * 1000,
                     r['new_balance_normalized'] if normalize else r['new_balance']
                    ) for r in result]
            })
        return results

    @dispatcher.add_method
    def get_chat_handle(wallet_id):
        result = mongo_db.chat_handles.find_one({"wallet_id": wallet_id})
        if not result: return False #doesn't exist
        result['last_touched'] = time.mktime(time.gmtime())
        mongo_db.chat_handles.save(result)
        data = {
            'handle': result['handle'],
            'op': result.get('op', False),
            'last_updated': result.get('last_updated', None)
            } if result else {}
        banned_until = result.get('banned_until', None) 
        if banned_until != -1 and banned_until is not None:
            data['banned_until'] = int(time.mktime(banned_until.timetuple())) * 1000 #convert to epoch ts in ms
        else:
            data['banned_until'] = banned_until #-1 or None
        return data

    @dispatcher.add_method
    def store_chat_handle(wallet_id, handle):
        """Set or update a chat handle"""
        if not isinstance(handle, basestring):
            raise Exception("Invalid chat handle: bad data type")
        if not re.match(r'[A-Za-z0-9_-]{4,12}', handle):
            raise Exception("Invalid chat handle: bad syntax/length")

        mongo_db.chat_handles.update(
            {'wallet_id': wallet_id},
            {"$set": {
                'wallet_id': wallet_id,
                'handle': handle,
                'last_updated': time.mktime(time.gmtime()),
                'last_touched': time.mktime(time.gmtime()) 
                }
            }, upsert=True)
        #^ last_updated MUST be in UTC, as it will be compaired again other servers
        return True

    @dispatcher.add_method
    def get_preferences(wallet_id):
        result =  mongo_db.preferences.find_one({"wallet_id": wallet_id})
        if not result: return False #doesn't exist
        result['last_touched'] = time.mktime(time.gmtime())
        mongo_db.preferences.save(result)
        return {
            'preferences': json.loads(result['preferences']),
            'last_updated': result.get('last_updated', None)
            } if result else {'preferences': {}, 'last_updated': None}

    @dispatcher.add_method
    def store_preferences(wallet_id, preferences):
        if not isinstance(preferences, dict):
            raise Exception("Invalid preferences object")
        try:
            preferences_json = json.dumps(preferences)
        except:
            raise Exception("Cannot dump preferences to JSON")
        
        #sanity check around max size
        if len(preferences_json) >= PREFERENCES_MAX_LENGTH:
            raise Exception("Preferences object is too big.")
        
        mongo_db.preferences.update(
            {'wallet_id': wallet_id},
            {"$set": {
                'wallet_id': wallet_id,
                'preferences': preferences_json,
                'last_updated': time.mktime(time.gmtime()),
                'last_touched': time.mktime(time.gmtime())
                }
            }, upsert=True)
        #^ last_updated MUST be in GMT, as it will be compaired again other servers
        return True
    
    @dispatcher.add_method
    def proxy_to_counterpartyd(method='', params=[]):
        result = None
        cache_key = None

        if redis_client: #check for a precached result and send that back instead
            cache_key = method + '||' + base64.b64encode(json.dumps(params).encode()).decode()
            #^ must use encoding (e.g. base64) since redis doesn't allow spaces in its key names
            # (also shortens the hashing key for better performance)
            result = redis_client.get(cache_key)
            if result:
                try:
                    result = json.loads(result)
                except Exception, e:
                    logging.warn("Error loading JSON from cache: %s, cached data: '%s'" % (e, result))
                    result = None #skip from reading from cache and just make the API call
        
        if result is None: #cache miss or cache disabled
            result = util.call_jsonrpc_api(method, params)
            if redis_client: #cache miss
                redis_client.setex(cache_key, DEFAULT_COUNTERPARTYD_API_CACHE_PERIOD, json.dumps(result))
                #^TODO: we may want to have different cache periods for different types of data
        
        if 'error' in result:
            errorMsg = result['error']['data'].get('message', result['error']['message'])
            raise Exception(errorMsg.encode('ascii','ignore'))
            #decode out unicode for now (json-rpc lib was made for python 3.3 and does str(errorMessage) internally,
            # which messes up w/ unicode under python 2.x)
        return result['result']


    class API(object):
        @cherrypy.expose
        def index(self):
            cherrypy.response.headers["Content-Type"] = 'application/json' 
            cherrypy.response.headers["Access-Control-Allow-Origin"] = '*'
            cherrypy.response.headers["Access-Control-Allow-Methods"] = 'POST, GET, OPTIONS'
            cherrypy.response.headers["Access-Control-Allow-Headers"] = 'Origin, X-Requested-With, Content-Type, Accept'

            if cherrypy.request.method == "OPTIONS": #web client will send us this before making a request
                return
            
            #don't do jack if we're not caught up
            if not config.CAUGHT_UP:
                raise cherrypy.HTTPError(525, 'Server is not caught up. Please try again later.')
                #^ 525 is a custom response code we use for this one purpose
            try:
                data = cherrypy.request.body.read().decode('utf-8')
            except ValueError:
                raise cherrypy.HTTPError(400, 'Invalid JSON document')
            response = JSONRPCResponseManager.handle(data, dispatcher)
            return json.dumps(response.data, default=util.json_dthandler).encode()
    
    cherrypy.config.update({
        'log.screen': False,
        "environment": "embedded",
        'log.error_log.propagate': False,
        'log.access_log.propagate': False,
        "server.logToScreen" : False
    })
    app_config = {
        '/': {
            'tools.trailing_slash.on': False,
        },
    }
    application = cherrypy.Application(API(), script_name="/api", config=app_config)

    #disable logging of the access and error logs to the screen
    application.log.access_log.propagate = False
    application.log.error_log.propagate = False
        
    #set up a rotating log handler for this application
    # Remove the default FileHandlers if present.
    application.log.error_file = ""
    application.log.access_file = ""
    maxBytes = getattr(application.log, "rot_maxBytes", 10000000)
    backupCount = getattr(application.log, "rot_backupCount", 1000)
    # Make a new RotatingFileHandler for the error log.
    fname = getattr(application.log, "rot_error_file", os.path.join(config.data_dir, "api.error.log"))
    h = logging_handlers.RotatingFileHandler(fname, 'a', maxBytes, backupCount)
    h.setLevel(logging.DEBUG)
    h.setFormatter(cherrypy._cplogging.logfmt)
    application.log.error_log.addHandler(h)
    # Make a new RotatingFileHandler for the access log.
    fname = getattr(application.log, "rot_access_file", os.path.join(config.data_dir, "api.access.log"))
    h = logging_handlers.RotatingFileHandler(fname, 'a', maxBytes, backupCount)
    h.setLevel(logging.DEBUG)
    h.setFormatter(cherrypy._cplogging.logfmt)
    application.log.access_log.addHandler(h)
    
    #start up the API listener/handler
    server = wsgi.WSGIServer((config.RPC_HOST, int(config.RPC_PORT)), application, log=None)
    server.serve_forever()
