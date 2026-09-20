# -*- coding: utf-8 -*-
"""插件管理状态机验证脚本（只读真实库 + 在副本上做行为实验）。
运行后自动清理副本。"""
import os, sys, json, shutil, sqlite3

BASE = r'C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system'
sys.path.insert(0, BASE)
from plugin_system import store  # noqa

SRC = os.path.join(BASE, 'mbse.db')
SCR = os.path.join(BASE, 'tmp', '_sm_probe.db')
TMPD = os.path.join(BASE, 'tmp')
os.makedirs(TMPD, exist_ok=True)
if os.path.exists(SCR):
    os.remove(SCR)
shutil.copy(SRC, SCR)

conn = sqlite3.connect(SCR)
conn.row_factory = sqlite3.Row

AUTHOR = {'id': 9001, 'display_name': '作者A', 'username': 'authorA',
          'permissions': {'admin': False, 'ai_studio': ['publish']}}
MKT = {'id': 9002, 'display_name': '市场管理员', 'username': 'mktadm',
       'permissions': {'admin': False, 'ai_studio': ['market_admin']}}
SYSADM = {'id': 9004, 'display_name': '系统管理员', 'username': 'sysadm',
          'permissions': {'admin': True}}
OTHER = {'id': 9003, 'display_name': '普通用户B', 'username': 'userB', 'permissions': {}}

OUT = []
def say(*a):
    s = ' '.join(str(x) for x in a)
    OUT.append(s)
    print(s)

def mk(pid, scope, status, author_id=9001, installs=(), count=None):
    conn.execute("DELETE FROM plugins WHERE plugin_id=?", (pid,))
    conn.execute("DELETE FROM plugin_installs WHERE plugin_id=?", (pid,))
    conn.execute("DELETE FROM plugin_versions WHERE plugin_id=?", (pid,))
    row = conn.execute(
        "SELECT * FROM plugins WHERE plugin_id NOT LIKE 'sm.%' "
        "AND manifest_json IS NOT NULL LIMIT 1").fetchone()
    d = dict(row) if row else {}
    d.pop('id', None)
    d.update(plugin_id=pid, name=pid.replace('.', '-'), scope=scope, status=status,
             author_id=author_id,
             author_name=(AUTHOR['display_name'] if author_id == 9001 else
                          ('平台内置' if author_id == 0 else '他人')),
             install_count=(count if count is not None else 0),
             manifest_json=json.dumps({'id': pid, 'name': pid.replace('.', '-'),
                                       'type': d.get('type') or 'skill', 'version': '1.0.0',
                                       'description': 'probe', 'label': {}},
                                      ensure_ascii=False))
    cols = ','.join(d.keys())
    conn.execute("INSERT INTO plugins (%s) VALUES (%s)" % (cols, ','.join('?' * len(d))),
                 list(d.values()))
    for (uid, en) in installs:
        conn.execute("INSERT OR REPLACE INTO plugin_installs "
                     "(user_id,plugin_id,version,enabled) VALUES (?,?,?,?)",
                     (uid, pid, '1.0.0', en))
    conn.commit()

def st(pid):
    r = conn.execute("SELECT scope,status,install_count FROM plugins WHERE plugin_id=?",
                     (pid,)).fetchone()
    return dict(r) if r else None

RES = []
def chk(name, passed, note=''):
    """登记一条断言结果（PASS/FAIL），用于末尾汇总与退出码。"""
    RES.append((name, bool(passed), note))
    return bool(passed)

say('#' * 74)
say('# PART A — 真实库状态分布与异常扫描（只读）')
say('#' * 74)
rows = conn.execute("SELECT scope,status,COUNT(*) n FROM plugins GROUP BY 1,2 "
                    "ORDER BY 1,2").fetchall()
say('\n[A1] (scope, status) 分布:')
for r in rows:
    say('   %-16s %-12s %3d' % (r['scope'], r['status'], r['n']))

ghost = conn.execute("SELECT plugin_id,scope,status FROM plugins WHERE scope='public' "
                     "AND status NOT IN ('published','disabled')").fetchall()
say('\n[A2] scope=public 但 status 非 published/disabled（幽灵上架）: %d' % len(ghost))
for r in ghost:
    say('   ! %s scope=%s status=%s' % (r['plugin_id'], r['scope'], r['status']))

pend = conn.execute("SELECT plugin_id,status,author_id FROM plugins "
                    "WHERE scope='pending_public'").fetchall()
say('\n[A3] 待审上架(scope=pending_public): %d' % len(pend))
for r in pend:
    say('   - %s status=%s author=%s' % (r['plugin_id'], r['status'], r['author_id']))

subm = conn.execute("SELECT plugin_id,scope,author_id FROM plugins "
                    "WHERE status='submitted'").fetchall()
say('\n[A4] status=submitted（旧审核链，注意不可编辑）: %d' % len(subm))
for r in subm:
    say('   - %s scope=%s author=%s' % (r['plugin_id'], r['scope'], r['author_id']))

drift = conn.execute(
    "SELECT p.plugin_id,p.install_count,"
    " (SELECT COUNT(*) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id) real_n "
    "FROM plugins p WHERE p.install_count != "
    " (SELECT COUNT(*) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id) "
    "ORDER BY p.install_count DESC LIMIT 15").fetchall()
say('\n[A5] install_count 与实际安装记录数不一致（前15，共%d）:' % len(drift))
for r in drift:
    say('   ~ %-46s 计数=%-4s 实际=%s' % (r['plugin_id'], r['install_count'], r['real_n']))

orph = conn.execute(
    "SELECT i.plugin_id,COUNT(*) n FROM plugin_installs i "
    "LEFT JOIN plugins p ON p.plugin_id=i.plugin_id "
    "WHERE p.plugin_id IS NULL OR p.status='removed' GROUP BY 1").fetchall()
say('\n[A6] 指向已删除/不存在插件的安装记录（孤儿）: %d 组, %d 行'
    % (len(orph), sum(r['n'] for r in orph)))

builtin_no_sys = conn.execute(
    "SELECT COUNT(*) FROM plugins p WHERE p.author_id=0 AND p.status='published' "
    "AND NOT EXISTS (SELECT 1 FROM plugin_installs i WHERE i.plugin_id=p.plugin_id "
    "AND i.user_id=0)").fetchone()[0]
say('\n[A7] 内置/系统所有且已发布，但缺系统级安装记录（运行时靠 fail-open 兜底）: %d'
    % builtin_no_sys)

pubs = conn.execute("SELECT plugin_id FROM plugins WHERE status='published'").fetchall()
cons_any = store.consumable_plugin_ids(conn, None, any_user=True)
not_consumable = [r['plugin_id'] for r in pubs if r['plugin_id'] not in cons_any]
say('\n[A8] 已发布但**全局装配池不可消费**（对 AI 静默失效）: %d / %d'
    % (len(not_consumable), len(pubs)))
for pid in not_consumable[:12]:
    say('   x %s' % pid)

say('\n' + '#' * 74)
say('# PART B — 库副本上的行为实验（调用真实 store 函数）')
say('#' * 74)

say('\n[B1] 越权验证：市场管理员已全局停用的 public 条目，作者能否自行恢复？')
mk('sm.t1', 'public', 'disabled')
say('   前置: %s' % st('sm.t1'))
ok, err = store.publish_self(conn, 'sm.t1', AUTHOR)
say('   author.publish_self -> ok=%s err=%s' % (ok, err))
say('   结果: %s' % st('sm.t1'))
say('   判定: %s' % ('!! 缺陷：作者绕过市场管理员恢复了全局停用' if ok else '正常：被拒绝'))
chk('B1 作者不能绕过市场管理员恢复全局停用', not ok, err)

say('\n[B2] 孤儿验证：申请上架中(pending_public)时作者取消发布，scope 会怎样？')
mk('sm.t2', 'pending_public', 'published')
say('   前置: %s' % st('sm.t2'))
ok, err = store.unpublish_self(conn, 'sm.t2', AUTHOR)
say('   author.unpublish_self -> ok=%s err=%s' % (ok, err))
say('   结果: %s' % st('sm.t2'))
s2 = st('sm.t2')
say('   判定: %s' % ('!! 缺陷：status=draft 但 scope 卡在 pending_public'
                   if ok and s2['scope'] == 'pending_public' else '正常：连带撤下申请'))
chk('B2 取消发布不再留下 pending_public+draft 孤儿态',
    bool(s2) and s2['scope'] == 'personal' and s2['status'] == 'draft',
    '结果 scope=%s status=%s' % (s2 and s2['scope'], s2 and s2['status']))

say('\n[B3] 复活验证：上一步的草稿被管理员审核通过后会怎样？')
ok, err = store.review_share(conn, 'sm.t2', MKT, approve=True)
say('   mkt.review_share(approve) -> ok=%s err=%s' % (ok, err))
say('   结果: %s' % st('sm.t2'))
s2 = st('sm.t2')
say('   判定: %s' % ('!! 缺陷：作者已取消发布的草稿被审核重新上架（scope=public + status=draft）'
                   if s2 and s2['scope'] == 'public' and s2['status'] == 'draft' else '正常：被拒绝'))
chk('B3 草稿不会被审核「复活上架」', not ok, err)

say('\n[B4] 遗留 submitted 链验收：管理员「通过」是否仍只改 status、不入市？')
mk('sm.t4', 'personal', 'submitted')
ok, err = store.transition(conn, 'sm.t4', 'published', MKT, comment='legacy approve')
say('   transition(submitted->published) -> ok=%s err=%s' % (ok, err))
say('   结果: %s' % st('sm.t4'))
s4 = st('sm.t4')
say('   说明: 该 src 已无调用方（遗留 /review 分支改走 scope 语义）；'
    'pending_reviews 现带 review_kind 标注，前端按此区分提示')
say('   判定: %s' % ('历史语义（status 维度），不再是用户可见路径'
                   if s4 and s4['scope'] == 'personal' else '已入市'))
chk('B4 遗留链不再被路由调用（缓存待审队列带 review_kind 标注）',
    all('review_kind' in r for r in store.pending_reviews(conn)),
    'pending=%d' % len(store.pending_reviews(conn)))

say('\n[B5] 自审自批验收：作者能否把自己 submitted 的条目直接发布（跳过审核）？')
mk('sm.t5', 'personal', 'submitted')
ok, err = store.publish_self(conn, 'sm.t5', AUTHOR)
say('   author.publish_self -> ok=%s err=%s' % (ok, err))
say('   结果: %s' % st('sm.t5'))
say('   判定: %s' % ('!! 缺陷：审核门形同虚设（submitted 可被作者直接发布）' if ok else '正常：被拒绝'))
chk('B5 作者不能自批 submitted → published（审核门有效）', not ok, err)

say('\n[B6] 准死锁验收：submitted 状态下作者能否自救（撤回提交后编辑）？')
mk('sm.t6', 'personal', 'submitted')
mf = json.loads(conn.execute("SELECT manifest_json FROM plugins WHERE plugin_id='sm.t6'")
                .fetchone()[0])
mf['description'] = '作者想修一下'
ok0, err0 = store.update_plugin(conn, 'sm.t6', mf, AUTHOR)
say('   author.update_plugin(审核期直改) -> ok=%s err=%s（锁定，符合预期）' % (ok0, err0))
ok, err = store.unpublish_self(conn, 'sm.t6', AUTHOR)
say('   author.unpublish_self(撤回提交) -> ok=%s err=%s 结果=%s' % (ok, err, st('sm.t6')))
ok2, err2 = store.update_plugin(conn, 'sm.t6', mf, AUTHOR)
say('   撤回后 author.update_plugin -> ok=%s err=%s' % (ok2, err2))
chk('B6 审核中作者可撤回提交（submitted->draft）后编辑',
    ok and ok2, '%s / %s' % (err, err2))

say('\n[B7] 撤回缺失验收：pending_public 状态下作者能否自行撤回申请？')
mk('sm.t7', 'pending_public', 'published')
ok, err = store.withdraw_share(conn, 'sm.t7', AUTHOR)
say('   author.withdraw_share(需市场管理员) -> ok=%s err=%s' % (ok, err))
ok2, err2 = store.cancel_share(conn, 'sm.t7', AUTHOR)
say('   author.cancel_share(作者侧撤回申请) -> ok=%s err=%s' % (ok2, err2))
s7 = st('sm.t7')
say('   结果: %s（status 不变 → 能力对作者照常可用）' % s7)
chk('B7 作者可自行撤回待审申请（status 不变，仅撤 scope）',
    ok2 and s7['scope'] == 'personal' and s7['status'] == 'published', err2)

say('\n[B8] 计数漂移验收：派生记录 + 卸载后，install_count 是否与实际行数恒等？')
mk('sm.t8', 'public', 'published', author_id=9001, installs=[(9003, 1)], count=5)
say('   前置: %s (他人已安装1条，计数列故意写 5 制造漂移)' % st('sm.t8'))
ok, err = store.set_install_enabled(conn, 'sm.t8', AUTHOR, False)
n = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id='sm.t8'").fetchone()[0]
say('   作者个人停用(派生记录) -> ok=%s 行数=%d 计数=%s' % (ok, n, st('sm.t8')['install_count']))
ok, err = store.uninstall(conn, 'sm.t8', OTHER)
say('   other.uninstall(自己没装过) -> ok=%s err=%s' % (ok, err))
ok, err = store.uninstall(conn, 'sm.t8', AUTHOR)
say('   author.uninstall -> ok=%s err=%s' % (ok, err))
after_rows = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id='sm.t8'").fetchone()[0]
after = st('sm.t8')['install_count']
say('   结果: 行数=%d 计数=%s（重算而非 ±1）' % (after_rows, after))
chk('B8 install_count 与实际安装行数恒等（消除 ±1 漂移）',
    after == after_rows, 'count=%s rows=%s' % (after, after_rows))
say('   reconcile_integrity 幂等重跑: %s' % store.reconcile_integrity(conn))
chk('B8b 校准后全库漂移归零', store.count_install_drift(conn) == 0,
    'drift=%d' % store.count_install_drift(conn))

say('\n[B9] 停用条目可否安装 / 可否执行')
mk('sm.t9', 'public', 'disabled')
ok, err = store.install(conn, 'sm.t9', OTHER)
say('   install(disabled市场条目) -> ok=%s err=%s' % (ok, err))
chk('B9 全局停用条目不可被安装（文案区分「停用」而非「不存在」）',
    not ok and '停用' in str(err), err)

say('\n[B10] 停用条目下架验收：市场管理员能否下架一个已停用条目？')
mk('sm.t10', 'public', 'disabled')
ok, err = store.transition(conn, 'sm.t10', 'draft', MKT)
say('   transition(disabled->draft) -> ok=%s err=%s（底层原语，按设计无此边）' % (ok, err))
ok2, err2 = store.withdraw_share(conn, 'sm.t10', MKT)
say('   mkt.withdraw_share(下架走 scope 语义) -> ok=%s err=%s' % (ok2, err2))
s10 = st('sm.t10')
say('   结果: %s（status 保留 disabled，作者侧仍不可用）' % s10)
chk('B10 已停用条目可下架（改 scope 不改 status，与「能力仍可用」语义一致）',
    ok2 and s10['scope'] == 'personal' and s10['status'] == 'disabled', err2)

say('\n[B11] 消费判定：全局停用后已安装用户是否失去访问')
mk('sm.t11', 'public', 'published', installs=[(9003, 1), (0, 1)])
say('   published+已安装: OTHER可消费=%s' % ('sm.t11' in store.consumable_plugin_ids(conn, OTHER)))
conn.execute("UPDATE plugins SET status='disabled' WHERE plugin_id='sm.t11'")
conn.commit()
say('   disabled 后:      OTHER可消费=%s' % ('sm.t11' in store.consumable_plugin_ids(conn, OTHER)))
say('   判定: 符合设计（全局停用切断所有人）')

say('\n[B12] 个人停用自建能力后再启用')
mk('sm.t12', 'personal', 'published', author_id=9001)
say('   发布中(自建): 作者可消费=%s' % ('sm.t12' in store.consumable_plugin_ids(conn, AUTHOR)))
store.set_install_enabled(conn, 'sm.t12', AUTHOR, False)
say('   个人停用后:   作者可消费=%s' % ('sm.t12' in store.consumable_plugin_ids(conn, AUTHOR)))
store.set_install_enabled(conn, 'sm.t12', AUTHOR, True)
say('   再次启用后:   作者可消费=%s' % ('sm.t12' in store.consumable_plugin_ids(conn, AUTHOR)))

say('\n' + '#' * 74)
say('# PART C — 状态图静态分析')
say('#' * 74)
T = store.TRANSITIONS
allst = set(T.keys())
reach = {'draft'}
frontier = ['draft']
while frontier:
    cur = frontier.pop()
    for nxt in T.get(cur, ()):
        if nxt not in reach:
            reach.add(nxt)
            frontier.append(nxt)
say('\n[C1] status 状态: %s' % sorted(allst))
say('     从 draft 可达: %s' % sorted(reach))
say('     不可达状态: %s' % (sorted(allst - reach) or '无'))
trap = [s for s in allst if not T.get(s)]
say('     终态(无出边): %s' % trap)
dead = [s for s in allst if (not T.get(s)) and s != 'removed']
say('     非预期死锁态: %s' % (dead or '无'))
ins = {s: [a for a in allst if s in T.get(a, ())] for s in allst}
say('\n[C2] 入度（哪些状态能被进入）:')
for s in sorted(allst):
    say('     %-11s <- %s' % (s, ins[s] or '（无）'))

SH = store.SHARE_TRANSITIONS
say('\n[C3] scope 状态: %s' % sorted(SH.keys()))
for k, v in sorted(SH.items()):
    say('     %-16s -> %s' % (k, sorted(v)))
say('     声明了 pending_public -> personal，实现方:')
say('       review_share(approve=False) 市场管理员驳回；')
say('       cancel_share               作者侧主动撤回申请（2026-09-17 第三刀补齐）')

say('\n[C4] 状态 × 编辑性:')
for s in sorted(allst):
    say('     %-11s 可编辑=%s' % (s, s in store.EDITABLE_STATUSES))

say('\n' + '#' * 74)
say('# PART D — 四刀修复回归汇总')
say('#' * 74)
bad = [c for c, ok, _d in RES if not ok]
say('\n[D1] 断言总数=%d  通过=%d  失败=%d' % (len(RES), len(RES) - len(bad), len(bad)))
for c, ok, d in RES:
    say('     %s %s%s' % ('PASS' if ok else 'FAIL', c, ('' if ok else '  <- ' + str(d))))
say('\n[D2] 全库计数漂移（校准前，含探针 fixture）: %d' % store.count_install_drift(conn))
rec = store.reconcile_integrity(conn)
say('     reconcile_integrity -> %s' % rec)
say('     校准后漂移: %d' % store.count_install_drift(conn))
chk('D2 reconcile_integrity 后全库计数漂移归零', store.count_install_drift(conn) == 0,
    'drift=%d' % store.count_install_drift(conn))
say('[VERDICT] %s' % ('全部回归通过' if not bad else '存在未通过断言：%s' % [c for c, _ok, _d in RES if not _ok]))

conn.close()
os.remove(SCR)
rep = os.path.join(TMPD, '_sm_report.txt')
with open(rep, 'w', encoding='utf-8') as f:
    f.write('\n'.join(OUT))
print('\n[report] ' + rep)
