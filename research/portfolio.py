"""Local fill ledger, with independent actual and simulated positions."""
from __future__ import annotations

from collections import deque
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json

from fastapi import HTTPException

from foundation import assets, database as store, retention
from research import sessions


MODES = {'actual', 'paper'}
PRICE_SCALE = 10000
FEE_SCALE = 100
MAX_SYMBOL_FILLS = 5000


def _check_mode(mode):
    if not isinstance(mode, str) or mode not in MODES:
        raise HTTPException(422, '请选择实际持仓或模拟账本')


def _units(value, scale, label, *, positive=False, optional=False):
    if optional and value in (None, ''):
        return None
    if type(value) not in (str, int, float) or len(str(value)) > 32:
        raise HTTPException(422, f'{label}须为有效金额')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 10_000_000 or positive and amount == 0:
            raise ValueError()
        ticks = amount * scale
        if ticks != ticks.to_integral_value():
            raise ValueError()
        return int(ticks)
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f'{label}超出可保存的金额范围或精度') from None


def _cash(value):
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if value is not None else None


def _parse(raw):
    if not isinstance(raw, dict) or set(raw) - {'external_id', 'symbol', 'side', 'filled_at', 'quantity', 'price', 'fees', 'cycle_id', 'note'}:
        raise HTTPException(422, '成交记录包含未知字段')
    external_id, side, quantity = raw.get('external_id'), raw.get('side'), raw.get('quantity')
    if not isinstance(external_id, str) or not 1 <= len(external_id.strip()) <= 80:
        raise HTTPException(422, '每笔成交须有可用于去重的编号')
    if side not in ('buy', 'sell') or type(quantity) is not int or not 1 <= quantity <= 100_000_000:
        raise HTTPException(422, '成交方向或股数无效')
    try:
        stamp = datetime.fromisoformat(raw['filled_at'])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=store.SHANGHAI)
        stamp = stamp.astimezone(store.SHANGHAI).isoformat(timespec='seconds')
        if stamp > store.now() or len(raw['filled_at']) < 16:
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(422, '请填写已发生的成交时间') from None
    note = raw.get('note', '')
    cycle_id = raw.get('cycle_id') or None
    if not isinstance(note, str) or len(note) > 300 or cycle_id is not None and (not isinstance(cycle_id, str) or len(cycle_id) != 16):
        raise HTTPException(422, '关联周期或备注无效')
    external_id = external_id.strip()
    return {
        'source_key': hashlib.sha256(external_id.encode()).hexdigest(), 'symbol': assets.symbol(raw.get('symbol')),
        'side': side, 'filled_at': stamp, 'quantity': quantity, 'price_ticks': _units(raw.get('price'), PRICE_SCALE, '成交价', positive=True),
        'fees_fen': _units(raw.get('fees'), FEE_SCALE, '成交总费用', optional=True), 'cycle_id': cycle_id,
        'data': store.dumps({'external_id': external_id, 'note': note.strip()}),
    }


def _rows(conn, mode, symbol):
    rows = conn.execute(
        'SELECT * FROM portfolio_fills WHERE mode=%s AND symbol=%s AND voided_at IS NULL ORDER BY filled_at,id LIMIT %s',
        (mode, symbol, MAX_SYMBOL_FILLS + 1),
    ).fetchall()
    if len(rows) > MAX_SYMBOL_FILLS:
        raise HTTPException(409, '单股成交记录超过本机复算上限')
    return rows


def _replay(rows, on_day):
    """FIFO. A cost with unreported fees remains unknown through partial sales."""
    lots = deque()
    realized = []
    for row in rows:
        day = row['filled_at'][:10]
        quantity = row['quantity']
        price = Decimal(row['price_ticks']) / PRICE_SCALE
        fees = Decimal(row['fees_fen']) / FEE_SCALE if row['fees_fen'] is not None else None
        if row['side'] == 'buy':
            lots.append({'quantity':quantity,'day':day,'price':price,'fees':fees})
            continue
        remainder = quantity
        gross_cost = Decimal(0)
        buy_fees = Decimal(0)
        while remainder:
            if not lots or lots[0]['day'] >= day:
                raise HTTPException(409, f"{row['symbol']} 在 {day} 的卖出超过此前已持有的股数，请核对成交顺序和买入记录")
            lot = lots[0]
            taken = min(remainder, lot['quantity'])
            gross_cost += taken * lot['price']
            if lot['fees'] is None:
                buy_fees = None
            else:
                allocation = lot['fees'] * taken / lot['quantity']
                lot['fees'] -= allocation
                if buy_fees is not None:
                    buy_fees += allocation
            remainder -= taken
            lot['quantity'] -= taken
            if lot['quantity'] == 0:
                lots.popleft()
        gross = quantity * price - gross_cost
        realized.append({'id':row['id'],'gross':gross,'net':gross-fees-buy_fees if fees is not None and buy_fees is not None else None})
    gross = sum((lot['quantity'] * lot['price'] for lot in lots), Decimal(0))
    all_fees = sum((lot['fees'] for lot in lots), Decimal(0)) if all(lot['fees'] is not None for lot in lots) else None
    return {'quantity':sum(lot['quantity'] for lot in lots),
            'available':sum(lot['quantity'] for lot in lots if lot['day'] < on_day),
            'gross_basis':gross, 'net_basis':gross + all_fees if all_fees is not None else None,
            'realized':realized}


def _mutex(conn, mode):
    key = 'portfolio_lock_' + mode
    conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)', (key,'0'))
    conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE', (key,)).fetchone()


def record(mode, raw):
    _check_mode(mode)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise HTTPException(422, '单次可录入1至100笔成交')
    items = [_parse(item) for item in raw]
    retention.ensure_capacity('portfolio')
    inserted, repeated = [], []
    with store.db() as conn:
        _mutex(conn, mode)
        days = {item['filled_at'][:10] for item in items}
        if any(sessions.trading_day(conn, day) is not True for day in days):
            raise HTTPException(409, '成交日期缺少已确认的交易日历')
        for item in items:
            old = conn.execute('SELECT * FROM portfolio_fills WHERE mode=%s AND source_key=%s', (mode,item['source_key'])).fetchone()
            if old:
                if old['voided_at'] or any(old[k]!=v for k,v in item.items()):
                    raise HTTPException(409, '成交编号已用于另一笔或已撤销的记录')
                repeated.append(old['id'])
                continue
            if item['cycle_id'] and not conn.execute('SELECT 1 FROM research_cycles WHERE id=%s AND symbol=%s', (item['cycle_id'],item['symbol'])).fetchone():
                raise HTTPException(422, '研究周期和成交股票不匹配')
            columns = tuple(item)
            result = conn.execute('INSERT INTO portfolio_fills(mode,created,' + ','.join(columns) + ') VALUES(' + ','.join(['%s']*(len(columns)+2)) + ')',
                                  (mode,store.now(),*(item[k] for k in columns)))
            inserted.append(result.lastrowid)
        for symbol in {item['symbol'] for item in items}:
            _replay(_rows(conn,mode,symbol),store.today())
        if inserted:
            store.audit('portfolio_fills_saved', {'mode':mode,'ids':inserted}, conn=conn)
    return {'inserted':inserted,'repeated':repeated}


def import_csv(mode, text):
    if not isinstance(text,str) or not 1<=len(text.encode())<=48000:
        raise HTTPException(422,'粘贴1至100笔成交，CSV正文最多48KB')
    required=['编号','代码','方向','成交时间','股数','成交价','费用']
    optional=['周期','备注']
    parsed=[]
    try:
        reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')),strict=True)
        names=reader.fieldnames or []
        if names not in (required,required+optional,required+['备注'],required+['周期']):
            raise ValueError()
        for row in reader:
            if None in row or any(row.get(k) is None for k in required) or len(parsed)>=100:
                raise ValueError()
            parsed.append({'external_id':row['编号'],'symbol':row['代码'],'side':{'买入':'buy','卖出':'sell'}.get(row['方向'].strip()),
                           'filled_at':row['成交时间'].strip(),'quantity':int(row['股数']),'price':row['成交价'].strip(),
                           'fees':row['费用'].strip(),'cycle_id':row.get('周期') or None,'note':row.get('备注') or ''})
    except (ValueError,csv.Error,TypeError):
        raise HTTPException(422,'CSV表头或成交行格式无效，请按导入窗口示例填写') from None
    return record(mode,parsed)


def void(mode, identifier, note):
    _check_mode(mode)
    if not isinstance(note,str) or not 1 <= len(note.strip()) <= 160:
        raise HTTPException(422, '请填写撤销原因')
    with store.db() as conn:
        _mutex(conn,mode)
        row = conn.execute('SELECT * FROM portfolio_fills WHERE id=%s AND mode=%s FOR UPDATE',(identifier,mode)).fetchone()
        if not row:
            raise HTTPException(404,'成交记录不存在')
        if row['voided_at']:
            return {'voided':True,'id':identifier}
        conn.execute('UPDATE portfolio_fills SET voided_at=%s WHERE id=%s',(store.now(),identifier))
        _replay(_rows(conn,mode,row['symbol']),store.today())
        store.audit('portfolio_fill_voided',{'mode':mode,'id':identifier,'note':note.strip()},conn=conn)
    return {'voided':True,'id':identifier}


def fills(mode, limit=25, before=None):
    _check_mode(mode)
    params = [mode]
    clause = ''
    with store.db(readonly=True) as conn:
        if before is not None:
            mark = conn.execute('SELECT filled_at,id FROM portfolio_fills WHERE id=%s AND mode=%s',(before,mode)).fetchone()
            if not mark:
                raise HTTPException(409, '成交分页游标已失效')
            clause = ' AND (filled_at,id)<(%s,%s)'
            params.extend((mark['filled_at'],mark['id']))
        rows = conn.execute('SELECT * FROM portfolio_fills WHERE mode=%s'+clause+' ORDER BY filled_at DESC,id DESC LIMIT %s', (*params,limit+1)).fetchall()
        results = []
        for row in rows[:limit]:
            data = json.loads(row['data'])
            results.append({k:row[k] for k in ('id','symbol','side','filled_at','quantity','cycle_id','voided_at')} |
                           {'external_id':data['external_id'],'note':data.get('note'),
                            'price':str(Decimal(row['price_ticks'])/PRICE_SCALE),
                            'fees':_cash(Decimal(row['fees_fen'])/FEE_SCALE) if row['fees_fen'] is not None else None})
        return {'items':results,'next':results[-1]['id'] if len(rows)>limit else None}


def positions(mode, limit=25, after=''):
    _check_mode(mode)
    if after:
        after=assets.symbol(after)
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        page = conn.execute("SELECT symbol FROM portfolio_fills WHERE mode=%s AND symbol>%s AND voided_at IS NULL "
                            "GROUP BY symbol HAVING SUM(CASE side WHEN 'buy' THEN quantity ELSE -CAST(quantity AS SIGNED) END)>0 "
                            "ORDER BY symbol LIMIT %s", (mode,after,limit+1)).fetchall()
        items = []
        for row in page[:limit]:
            symbol = row[0]
            position = _replay(_rows(conn,mode,symbol),store.today())
            bar = conn.execute("SELECT h.day,b.close FROM heads h JOIN daily_bars b ON b.fact_id=h.fact_id "
                               "WHERE h.dataset='daily' AND h.symbol=%s AND h.day<=%s ORDER BY h.day DESC LIMIT 1",(symbol,store.today())).fetchone()
            close = bar['close'] if bar else None
            mark = Decimal(str(close)) * position['quantity'] if close is not None else None
            cost = position['net_basis']
            items.append({'symbol':symbol,'quantity':position['quantity'],'available':position['available'],
                          'gross_basis':_cash(position['gross_basis']),'net_basis':_cash(cost),'reference_value':_cash(mark),
                          'reference_day':bar['day'] if bar else None,'unrealized_net':_cash(mark-cost) if mark is not None and cost is not None else None})
        return {'items':items,'next':items[-1]['symbol'] if len(page)>limit else None}


def symbol_summary(mode, symbol):
    _check_mode(mode)
    symbol=assets.symbol(symbol)
    with store.db(readonly=True) as conn:
        rows=_rows(conn,mode,symbol)
    results=_replay(rows,store.today())['realized']
    net=sum((r['net'] for r in results),Decimal(0)) if results and all(r['net'] is not None for r in results) else None
    gross=sum((r['gross'] for r in results),Decimal(0)) if results else None
    return {'symbol':symbol,'sells':len(results),'realized_gross':_cash(gross),'realized_net':_cash(net),
            'known_net_sells':sum(r['net'] is not None for r in results)}
