"""场景登记。

每个场景单独一份：网站、是否挂岗位、页面动作是否已接入。
任务保存、岗位资料、草案发布和节奏由各场景共用。
寻访场景按网站复制，例如 BOSS 与猎聘各是一个场景、一个任务。
"""

SCENARIOS = {
    'web': {
        'engine': 'web',
        'kind': 'web',
        'label': '通用网页 · 公开文字',
        'hosts': (),
        'platform': None,
        'default_website': '',
        'default_scenario': '',
        'page_adapter': None,
    },
    'boss_sourcing': {
        'engine': 'boss_sourcing',
        'kind': 'sourcing',
        'label': 'BOSS · 脱敏寻访',
        'hosts': ('zhipin.com',),
        'platform': 'boss',
        'default_website': 'https://www.zhipin.com/',
        'default_scenario': '候选人寻访与跟进',
        'page_adapter': 'boss',
    },
    # 与 BOSS 寻访同一套任务和岗位设施，网站、场景和任务分开。页面动作尚未接入。
    'liepin_sourcing': {
        'engine': 'liepin_sourcing',
        'kind': 'sourcing',
        'label': '猎聘 · 脱敏寻访',
        'hosts': ('liepin.com',),
        'platform': 'liepin',
        'default_website': 'https://www.liepin.com/',
        'default_scenario': '候选人寻访与跟进',
        'page_adapter': None,
    },
}


def scenario_for_engine(engine):
    item = SCENARIOS.get(engine)
    if not item:
        raise ValueError('不支持的场景适配器')
    return item


def is_sourcing(engine):
    item = SCENARIOS.get(engine)
    return bool(item and item['kind'] == 'sourcing')


def _host_matches(hostname, roots):
    host = (hostname or '').lower()
    return any(host == root or host.endswith('.' + root) for root in roots)


def scenario_for_host(hostname):
    for item in SCENARIOS.values():
        if item['kind'] == 'sourcing' and _host_matches(hostname, item['hosts']):
            return item
    return None


def scenario_for_platform(platform):
    for item in SCENARIOS.values():
        if item['kind'] == 'sourcing' and item['platform'] == platform:
            return item
    return None
