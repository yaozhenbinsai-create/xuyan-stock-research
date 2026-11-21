"""Late-discovered disclosures must invalidate risk checks by availability time."""
import json,re,unittest
from unittest.mock import patch
from research import evidence

class Result:
    def __init__(self,rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows

class AvailableNotices:
    def __init__(self,rows):self.rows=rows;self.last_sql='';self.last_params=()
    def execute(self,sql,params=()):
        if sql.startswith('SELECT COUNT(*)'):return Result([(1,)])
        self.last_sql=sql;self.last_params=params
        # Model the relevant SQL predicates, including the previous publication-day
        # filter and the previous 30-row truncation, using distinct dated notices.
        symbol,start,cutoff,pattern=params
        assert 'h.day>=' not in sql
        assert "REGEXP %s" in sql and 'LIMIT 1' in sql
        available=[r for r in self.rows if r['symbol']==symbol and start<r['first_seen']<=cutoff and re.search(pattern,r['title'])]
        return Result([{'title':r['title']} for r in available[:1]])

class RiskInvalidationAvailabilityTests(unittest.TestCase):
    def result(self,rows):
        check={'state':'verified','fact_ids':[1],'confirmed_at':'2026-11-13T10:00:00+08:00','valid_until':'2026-11-14','note':'已有原文核验'}
        db=AvailableNotices(rows)
        with patch.object(evidence.store,'setting',side_effect=lambda key,*a,**k:check if key.endswith(':risk') else {}):
            result=evidence.read_checks(db,'sh.600000','2026-11-13T12:00:00+08:00')
        return result,db
    def notice(self,title='重大诉讼公告',day='2026-11-12',seen='2026-11-13T11:00:00+08:00'):
        return {'symbol':'sh.600000','day':day,'first_seen':seen,'title':title}
    def test_old_publication_newly_discovered_after_check_is_blocked(self):
        result,db=self.result([self.notice()])
        self.assertEqual(result['risk']['state'],'blocked')
        self.assertEqual(db.last_params[1:3],('2026-11-13T10:00:00+08:00','2026-11-13T12:00:00+08:00'))
    def test_risk_not_hidden_by_more_than_thirty_routine_notices(self):
        result,_=self.result([self.notice('董事会会议决议') for _ in range(35)]+[self.notice()])
        self.assertEqual(result['risk']['state'],'blocked')
    def test_unavailable_future_notice_does_not_invalidate_frozen_check(self):
        result,_=self.result([self.notice(seen='2026-11-13T12:01:00+08:00')])
        self.assertEqual(result['risk']['state'],'verified')
    def test_routine_new_notice_does_not_create_risk_conflict(self):
        result,_=self.result([self.notice('董事会会议决议')])
        self.assertEqual(result['risk']['state'],'verified')

if __name__=='__main__':unittest.main()
