#!/usr/bin/env python3
"""
BOSS直聘单职位详情抓取工具 (CDP版 - 纯DOM解析)

通过 CDP 连接 Chrome，导航到 BOSS 直聘页面后，
仅通过 DOM 解析提取结构化数据，不调用任何 API。

数据源分工:
  - 职位详情页(job.html): 公司名、成立日期、注册资金、规模、融资、行业、地址、
    经纬度、职位名称、薪资、经验、学历、技能、职责、要求、福利、薪资构成
  - 公司介绍页(company.html): 公司介绍、工作时间、经营范围

输出格式对齐 references/jd_fields.md 的结构化字段定义，
保存到 jobs/<id>/ 目录。

用法:
  python3 jd_cdp_parser.py --setup-chrome
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html"
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --debug
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --no-company
  python3 jd_cdp_parser.py --check
"""
__version__ = "4.0.0"

import json
import time
import random
import sys
import argparse
import os
import re
import html
import platform
import subprocess
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jd_cdp")

DEFAULT_CDP_PORT = 9222

websocket = None
requests = None


def require_runtime_dependencies(*names):
    global requests, websocket
    missing = []
    if "requests" in names and requests is None:
        try:
            import requests as _r
            requests = _r
        except ImportError:
            missing.append("requests")
    if "websocket" in names and websocket is None:
        try:
            import websocket as _w
            websocket = _w
        except ImportError:
            missing.append("websocket-client")
    if missing:
        print(f"缺少依赖: {' '.join(missing)}")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True


def get_default_chrome_path():
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if system == "Windows":
        import ntpath
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = ntpath.join(local, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(p):
                return p
        return "chrome.exe"
    for c in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
        if os.path.exists(c):
            return c
    return "google-chrome"


DEFAULT_CHROME_PATH = get_default_chrome_path()
DEFAULT_CDP_DATA_DIR = os.path.expanduser("~/.boss-zhipin-scraper/cdp-profile")


# ============================================================
# CDP 连接
# ============================================================
class CDPSession:
    def __init__(self, cdp_port=DEFAULT_CDP_PORT):
        if not require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
        self.cdp_port = cdp_port
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=10)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.mid = 0

    def send(self, method, params=None, sid=None, timeout=30):
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))

        start_time = time.time()
        max_retries = 1000
        for attempt in range(max_retries):
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"CDP send({method}) 超时 ({timeout}s), "
                    f"已跳过 {attempt} 条不匹配消息"
                )
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                raise TimeoutError(f"CDP WebSocket recv 超时, method={method}")
            try:
                r = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("id") == self.mid:
                return r
            event_name = r.get("method", "unknown")
            log.debug(f"跳过不匹配消息 (id={r.get('id')}, event={event_name})")
        raise TimeoutError(
            f"CDP send({method}) 在 {max_retries} 条消息内未找到匹配响应"
        )

    def eval_js(self, js, sid):
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True}, sid)
        result_obj = r.get("result", {}).get("result", {})
        if result_obj.get("subtype") == "error":
            desc = result_obj.get("description", "unknown JS error")
            log.error(f"JS eval error: {desc}")
            return None
        exception = r.get("result", {}).get("exceptionDetails")
        if exception:
            desc = exception.get("exception", {}).get("description", exception.get("text", "unknown"))
            log.error(f"JS exception: {desc}")
            return None
        return result_obj.get("value", None)

    def close(self):
        self.ws.close()


# ============================================================
# 职位详情页 DOM 提取 JS
# ============================================================
EXTRACT_JD_PAGE_JS = """
(function(){
    var result = {};
    // 优先取 _jobInfo
    var jobInfo = window._jobInfo || {};
    result.job_name = jobInfo.job_name || '';
    result.job_salary = jobInfo.job_salary || '';
    result.company_short_name = jobInfo.company || '';

    // 侧边栏工商信息
    // 公司全称: ul.level-list > li.company-name
    var companyNameEl = document.querySelector('.level-list .company-name');
    if (companyNameEl) {
        var text = companyNameEl.innerText.trim();
        result.company_name = text.replace(/^公司名称/, '').trim();
    } else {
        result.company_name = '';
    }

    // 成立日期: li.res-time
    var resTimeEl = document.querySelector('.level-list .res-time');
    if (resTimeEl) {
        var text = resTimeEl.innerText.trim();
        result.company_founding_date = text.replace(/^成立日期/, '').trim();
    } else {
        result.company_founding_date = '';
    }

    // 注册资金: li.company-fund
    var fundEl = document.querySelector('.level-list .company-fund');
    if (fundEl) {
        var text = fundEl.innerText.trim();
        var numMatch = text.match(/(\\d+\\.?\\d*)/);
        result.company_registered_capital = numMatch ? parseFloat(numMatch[1]) : null;
    } else {
        result.company_registered_capital = null;
    }

    // 公司规模: i.icon-scale 的父级 p 文本
    var scaleIcon = document.querySelector('.icon-scale');
    if (scaleIcon) {
        var pEl = scaleIcon.parentElement;
        if (pEl) {
            // 获取 p 的纯文本，去掉 icon 元素自身内容
            result.company_scale = pEl.textContent.trim().replace(/[\\s\\n]/g, '');
        } else {
            result.company_scale = '';
        }
    } else {
        result.company_scale = '';
    }

    // 公司融资阶段: i.icon-stage 的父级 p 文本
    var stageIcon = document.querySelector('.icon-stage');
    if (stageIcon) {
        var pEl = stageIcon.parentElement;
        if (pEl) {
            result.company_financing_stage = pEl.textContent.trim().replace(/[\\s\\n]/g, '');
        } else {
            result.company_financing_stage = '';
        }
    } else {
        result.company_financing_stage = '';
    }

    // 公司所属行业: i.icon-industry 的链接文本
    var industryIcon = document.querySelector('.icon-industry');
    if (industryIcon) {
        var linkEl = industryIcon.parentElement.querySelector('a');
        if (linkEl) {
            result.company_industry = linkEl.textContent.trim();
        } else {
            result.company_industry = industryIcon.parentElement.textContent.trim().replace(/[\\s\\n]/g, '');
        }
    } else {
        result.company_industry = '';
    }

    // 公司地址: .location-address
    var addrEl = document.querySelector('.location-address');
    result.company_address = addrEl ? addrEl.textContent.trim() : '';

    // 经纬度: .job-location-map 的 data-lat 属性
    var mapEl = document.querySelector('.job-location-map');
    result.company_lat_lng = mapEl ? mapEl.getAttribute('data-lat') || '' : '';

    // 职位名称: 优先 _jobInfo.job_name，兜底 h1
    if (!result.job_name) {
        var h1El = document.querySelector('.name h1') || document.querySelector('h1');
        result.job_name = h1El ? h1El.textContent.trim() : '';
    }

    // 薪资原文: 优先 _jobInfo.job_salary，兜底 .name .salary
    if (!result.job_salary) {
        var salaryEl = document.querySelector('.name .salary') || document.querySelector('.salary');
        result.job_salary = salaryEl ? salaryEl.textContent.trim() : '';
    }

    // 经验要求: .text-desc.text-experiece (注意拼写)
    var expEl = document.querySelector('.text-desc.text-experiece');
    result.experience_required = expEl ? expEl.textContent.trim() : '';

    // 学历要求: .text-desc.text-degree
    var degEl = document.querySelector('.text-desc.text-degree');
    result.education_required = degEl ? degEl.textContent.trim() : '';

    // 福利标签: .tag-container-new > .job-tags span (直接子元素避免重复)
    var benefits = [];
    var jobTagsSpans = document.querySelectorAll('.tag-container-new > .job-tags span');
    for (var i = 0; i < jobTagsSpans.length; i++) {
        var t = jobTagsSpans[i].textContent.trim();
        if (t && t !== '...' && benefits.indexOf(t) === -1) benefits.push(t);
    }
    result.benefits = benefits;

    // 技能关键词: .job-keyword-list li（过滤 BOSS 推荐噪声）
    var skills = [];
    var noisePrefixes = ['来自BOSS直聘', 'BOSS直聘', 'BOSS推荐'];
    var skillLis = document.querySelectorAll('.job-keyword-list li');
    for (var i = 0; i < skillLis.length; i++) {
        var t = skillLis[i].textContent.trim();
        for (var j = 0; j < noisePrefixes.length; j++) {
            if (t.indexOf(noisePrefixes[j]) === 0) {
                t = t.substring(noisePrefixes[j].length).trim();
            }
        }
        if (t && skills.indexOf(t) === -1) skills.push(t);
    }
    result.skill_tags = skills;

    // 职位描述: 第一个 .job-sec-text (不含 fold-text 的那个)
    var secTexts = document.querySelectorAll('.job-sec-text');
    var jdFullText = '';
    for (var i = 0; i < secTexts.length; i++) {
        if (!secTexts[i].classList.contains('fold-text')) {
            jdFullText = secTexts[i].textContent.trim();
            break;
        }
    }
    // 如果没找到不含 fold-text 的，取第一个
    if (!jdFullText && secTexts.length > 0) {
        jdFullText = secTexts[0].textContent.trim();
    }
    result.jd_full = jdFullText;

    // 薪资构成: 从薪资原文中匹配关键词
    var salaryComp = '';
    var compKeywords = ['14薪', '16薪', '13薪', '15薪', '期权', '股票', '股权'];
    for (var i = 0; i < compKeywords.length; i++) {
        if (result.job_salary.indexOf(compKeywords[i]) !== -1) {
            salaryComp = compKeywords[i];
            break;
        }
    }
    // 也匹配 "·13薪" 格式
    var compMatch = result.job_salary.match(/[·]\\s*(\\d+薪|期权|股票|股权)/);
    if (compMatch && !salaryComp) {
        salaryComp = compMatch[1];
    }
    result.salary_composition = salaryComp;

    // 公司页链接: 优先取带 ka 属性的具体公司链接，避免取到导航栏通用链接
    var companyLink = '';
    // 优先级 1: 带有 job-detail 相关 ka 属性的链接
    var prioritySelectors = [
        'a[ka="job-detail-company_custompage"]',
        'a[ka="job-detail-company-logo_custompage"]',
        'a[ka="job-cominfo"]',
        'a[ka="job-comintroduce"]'
    ];
    for (var i = 0; i < prioritySelectors.length; i++) {
        var el = document.querySelector(prioritySelectors[i]);
        if (el && el.href && el.href.indexOf('/gongsi/') !== -1 && el.href.indexOf('/job/') === -1) {
            companyLink = el.href;
            break;
        }
    }
    // 优先级 2: 含品牌 ID 的 /gongsi/ 链接 (带 encryptBrandId 格式)
    if (!companyLink) {
        var gongsiLinks = document.querySelectorAll('a[href*="/gongsi/"]');
        for (var i = 0; i < gongsiLinks.length; i++) {
            var href = gongsiLinks[i].href || '';
            // 排除导航栏通用链接 (只含 /gongsi/ 不含 ID) 和职位列表链接
            if (href && href.indexOf('/job/') === -1 && href.match(/gongsi\\/[\\w]+\\./)) {
                companyLink = href;
                break;
            }
        }
    }
    result.company_url = companyLink;

    result.url = location.href;

    return JSON.stringify(result);
})()
"""

# ============================================================
# 公司介绍页 DOM 提取 JS
# ============================================================
EXTRACT_COMPANY_PAGE_JS = """
(function(){
    var result = {};

    // 公司介绍: .company-info-box .fold-text
    var introEl = document.querySelector('.company-info-box .fold-text');
    if (!introEl) {
        introEl = document.querySelector('#main .company-info-box .fold-text');
    }
    if (!introEl) {
        // 兜底: .job-sec .fold-text (但需要排除其他 job-sec)
        var foldTexts = document.querySelectorAll('.company-info-box .fold-text, .job-sec .fold-text');
        for (var i = 0; i < foldTexts.length; i++) {
            introEl = foldTexts[i];
            break;
        }
    }
    result.company_intro = introEl ? introEl.textContent.trim() : '';

    // 工作时间: .work-time p
    var workTimeP = document.querySelector('.work-time p');
    if (workTimeP) {
        // p 里面可能有 img 和 span，取 span 文本或整体文本
        var spanEl = workTimeP.querySelector('span');
        if (spanEl) {
            result.working_hours = spanEl.textContent.trim();
        } else {
            result.working_hours = workTimeP.textContent.trim();
        }
    } else {
        result.working_hours = '';
    }

    // 经营范围: .business-detail li.col-auto 中包含 "经营范围" 的那个
    result.business_scope = '';
    var colAutoLis = document.querySelectorAll('.business-detail li.col-auto');
    for (var i = 0; i < colAutoLis.length; i++) {
        var liText = colAutoLis[i].textContent.trim();
        if (liText.indexOf('经营范围') !== -1) {
            result.business_scope = liText.replace(/^经营范围[：:]\\s*/, '').trim();
            break;
        }
    }

    // _brandInfo (可选，用于跨页关联)
    var brandInfo = window._brandInfo || {};
    result.brand_id = brandInfo.brand_id || '';

    result.url = location.href;

    return JSON.stringify(result);
})()
"""


# ============================================================
# 薪资解析
# ============================================================
def parse_salary(raw_salary):
    """解析薪资文本，返回 (下限万/年, 上限万/年, 薪资构成)

    如 '25-40K' -> (30.0, 48.0, '')
    如 '15-25K·14薪' -> (18.0, 30.0, '14薪')
    如 '1-2万/月' -> (12.0, 24.0, '')
    """
    if not raw_salary:
        return None, None, ''

    raw = raw_salary.strip()
    salary_composition = ''

    # 先提取薪资构成: "·14薪" 等
    comp_match = re.search(r'[·\s]+(\d+薪|期权|股票|股权)', raw)
    if comp_match:
        salary_composition = comp_match.group(1)
        raw = raw[:comp_match.start()].strip()

    # K 格式: "25-40K"
    k_match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K', raw, re.IGNORECASE)
    if k_match:
        low = float(k_match.group(1)) * 12 / 10
        high = float(k_match.group(2)) * 12 / 10
        return low, high, salary_composition

    # 万/月 格式: "1-2万/月"
    wan_match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*万', raw)
    if wan_match:
        low = float(wan_match.group(1)) * 12
        high = float(wan_match.group(2)) * 12
        return low, high, salary_composition

    # 元/月 格式
    yuan_match = re.search(r'(\d+)\s*-\s*(\d+)\s*元', raw)
    if yuan_match:
        low = float(yuan_match.group(1)) * 12 / 10000
        high = float(yuan_match.group(2)) * 12 / 10000
        return low, high, salary_composition

    return None, None, salary_composition


# ============================================================
# JD 描述拆分: 职责 vs 要求
# ============================================================
def split_jd_description(full_text):
    """将 JD 全文拆分为岗位职责和任职要求

    按 '任职资格[：:]' 分割，前半段为职责，后半段为要求。
    """
    if not full_text:
        return '', ''

    parts = re.split(r'任职资格[：:]', full_text, maxsplit=1)
    responsibility = parts[0]
    requirement = parts[1] if len(parts) > 1 else ''

    # 去掉开头的标签
    responsibility = responsibility.replace('工作职责：', '').replace('工作职责:', '').strip()
    # 也处理 "岗位职责：" 标签
    responsibility = responsibility.replace('岗位职责：', '').replace('岗位职责:', '').strip()
    requirement = requirement.strip()

    return responsibility, requirement


# ============================================================
# 构建结构化输出
# ============================================================
def build_structured(jd_data, company_data=None):
    """从 JD 页和公司页 DOM 数据构建最终结构化 JSON

    jd_data: 从 EXTRACT_JD_PAGE_JS 提取的数据
    company_data: 从 EXTRACT_COMPANY_PAGE_JS 提取的数据 (可选)
    """
    result = {}

    # ---- 来自 JD 页的字段 ----
    result['company_name'] = jd_data.get('company_name', '') or jd_data.get('company_short_name', '') or ''
    result['company_founding_date'] = jd_data.get('company_founding_date', '') or ''
    result['company_registered_capital'] = jd_data.get('company_registered_capital') or 0
    result['company_scale'] = jd_data.get('company_scale', '') or ''
    result['company_financing_stage'] = jd_data.get('company_financing_stage', '') or ''
    result['company_industry'] = jd_data.get('company_industry', '') or ''
    result['company_address'] = jd_data.get('company_address', '') or ''
    result['company_lat_lng'] = jd_data.get('company_lat_lng', '') or ''
    result['job_name'] = jd_data.get('job_name', '') or ''
    result['experience_required'] = jd_data.get('experience_required', '') or ''
    result['education_required'] = jd_data.get('education_required', '') or ''
    result['skill_tags'] = jd_data.get('skill_tags', []) or []
    result['benefits'] = jd_data.get('benefits', []) or []

    # 薪资解析
    raw_salary = jd_data.get('job_salary', '') or ''
    salary_low, salary_high, salary_comp = parse_salary(raw_salary)
    result['job_salary_low_10k'] = salary_low if salary_low is not None else 0
    result['job_salary_high_10k'] = salary_high if salary_high is not None else 0
    # 薪资构成: 优先从 JD 提取的结果，或从薪资原文解析的
    result['salary_composition'] = jd_data.get('salary_composition', '') or salary_comp or ''

    # JD 描述拆分
    jd_full = jd_data.get('jd_full', '') or ''
    responsibility, requirement = split_jd_description(jd_full)
    result['job_responsibility'] = responsibility
    result['job_requirement'] = requirement

    # ---- 来自公司页的字段 (如果有) ----
    result['company_intro'] = ''
    result['working_hours'] = ''
    result['business_scope'] = ''

    if company_data:
        result['company_intro'] = company_data.get('company_intro', '') or ''
        result['working_hours'] = company_data.get('working_hours', '') or ''
        result['business_scope'] = company_data.get('business_scope', '') or ''

    # ---- 附加元数据 ----
    result['_source_url'] = jd_data.get('url', '') or ''
    result['_company_url'] = jd_data.get('company_url', '') or ''
    result['_brand_id'] = company_data.get('brand_id', '') if company_data else ''

    return result


# ============================================================
# 抓取核心逻辑
# ============================================================
def _human_simulate(ws, sid):
    scroll_count = random.randint(3, 7)
    for i in range(scroll_count):
        if random.random() < 0.12:
            delta = -random.randint(80, 200)
        else:
            delta = random.randint(200, 600)
        ws.eval_js(f"window.scrollBy(0,{delta})", sid)
        if random.random() < 0.35:
            time.sleep(random.uniform(2.0, 5.0))
        else:
            time.sleep(random.uniform(0.8, 1.8))

    if random.random() < 0.5:
        ws.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": random.randint(200, 800),
            "y": random.randint(200, 600)
        }, sid)
        time.sleep(random.uniform(0.5, 1.5))


def _safe_json_loads(val, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(val) if isinstance(val, str) else default
    except (json.JSONDecodeError, ValueError, TypeError):
        return default


def scrape_jd_page(ws, sid, url, debug=False):
    """加载 JD 页面，纯 DOM 解析提取数据"""
    print(f"  [JD页] 加载: {url}")
    ws.send("Page.navigate", {"url": url}, sid)
    time.sleep(random.uniform(5, 10))

    print(f"  [JD页] 模拟阅读...")
    _human_simulate(ws, sid)

    print(f"  [JD页] DOM 解析提取...")
    raw_text = ws.eval_js(EXTRACT_JD_PAGE_JS, sid)
    jd_data = _safe_json_loads(raw_text)

    if debug:
        print("\n" + "=" * 60)
        print("JD 页 DOM 提取数据:")
        for k, v in jd_data.items():
            v_str = str(v)
            if len(v_str) > 150:
                v_str = v_str[:150] + "..."
            print(f"  {k}: {v_str}")
        print("=" * 60 + "\n")

    return jd_data


def scrape_company_page(ws, sid, url, debug=False):
    """加载公司页面，纯 DOM 解析提取公司介绍、工作时间、经营范围"""
    print(f"  [公司页] 加载: {url}")
    ws.send("Page.navigate", {"url": url}, sid)
    time.sleep(random.uniform(4, 8))

    print(f"  [公司页] 模拟阅读...")
    _human_simulate(ws, sid)

    print(f"  [公司页] DOM 解析提取...")
    raw_text = ws.eval_js(EXTRACT_COMPANY_PAGE_JS, sid)
    company_data = _safe_json_loads(raw_text)

    if debug:
        print("\n" + "=" * 60)
        print("公司页 DOM 提取数据:")
        for k, v in company_data.items():
            v_str = str(v)
            if len(v_str) > 150:
                v_str = v_str[:150] + "..."
            print(f"  {k}: {v_str}")
        print("=" * 60 + "\n")

    return company_data


def scrape_jd(url, cdp_port=DEFAULT_CDP_PORT, debug=False, fetch_company=True):
    """抓取 JD 页 + 公司页，纯 DOM 解析"""
    ws = CDPSession(cdp_port)
    tid = None

    try:
        r = ws.send("Target.createTarget", {"url": "about:blank"})
        tid = r["result"]["targetId"]
        r = ws.send("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = r["result"]["sessionId"]

        # ---- JD 页 ----
        jd_data = scrape_jd_page(ws, sid, url, debug=debug)

        # ---- 公司页 ----
        company_data = None
        company_url = jd_data.get('company_url', '')

        if fetch_company and company_url:
            time.sleep(random.uniform(3, 6))
            try:
                company_data = scrape_company_page(ws, sid, company_url, debug=debug)
            except Exception as e:
                print(f"  ⚠️ 公司页抓取失败: {e}")
        elif fetch_company and not company_url:
            print(f"  ⚠️ 未找到公司页链接，跳过")

        # ---- 构建结构化数据 ----
        structured = build_structured(jd_data, company_data)

        if debug:
            print("=" * 60)
            print("最终结构化结果:")
            print("=" * 60)
            print(json.dumps(structured, ensure_ascii=False, indent=2))
            print("=" * 60 + "\n")

        return {
            'jd_data': jd_data,
            'company_data': company_data,
            'structured': structured,
        }
    finally:
        if tid:
            try:
                ws.send("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass
        ws.close()


# ============================================================
# 文件保存
# ============================================================
def get_next_job_id(jobs_dir):
    if not os.path.exists(jobs_dir):
        os.makedirs(jobs_dir, exist_ok=True)
        return 1
    existing = []
    for name in os.listdir(jobs_dir):
        if os.path.isdir(os.path.join(jobs_dir, name)):
            num_match = re.search(r'(\d+)', name)
            if num_match:
                existing.append(int(num_match.group(1)))
    return max(existing) + 1 if existing else 1


def _write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_text(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def save_to_jobs_dir(result, jobs_root='jobs'):
    jobs_root = os.path.abspath(jobs_root)
    next_id = get_next_job_id(jobs_root)
    job_dir = os.path.join(jobs_root, f"{next_id:03d}")
    os.makedirs(job_dir, exist_ok=True)

    structured = result.get('structured', {})
    jd_data = result.get('jd_data', {})
    company_data = result.get('company_data')

    # 1) 结构化数据 (对齐 jd_fields.md)
    _write_json(os.path.join(job_dir, 'structured.json'), structured)

    # 2) JD 页原始 DOM 提取数据
    _write_json(os.path.join(job_dir, 'jd_dom_data.json'), jd_data)

    # 3) 公司页原始 DOM 提取数据
    if company_data:
        _write_json(os.path.join(job_dir, 'company_dom_data.json'), company_data)

    # 4) JD 全文
    jd_full = jd_data.get('jd_full', '') or ''
    if jd_full:
        _write_text(os.path.join(job_dir, 'jd_full.txt'), jd_full)

    # 5) 公司介绍全文
    company_intro = company_data.get('company_intro', '') if company_data else ''
    if company_intro:
        _write_text(os.path.join(job_dir, 'company_intro.txt'), company_intro)

    # 6) 元数据
    meta = {
        'job_id': next_id,
        'job_dir': job_dir,
        'source_url': structured.get('_source_url', ''),
        'company_url': structured.get('_company_url', ''),
        'crawled_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'job_title': structured.get('job_name', ''),
        'company_name': structured.get('company_name', ''),
        'brand_id': structured.get('_brand_id', ''),
    }
    _write_json(os.path.join(job_dir, 'meta.json'), meta)

    return job_dir, next_id


# ============================================================
# 环境检查
# ============================================================
def is_cdp_ready(cdp_port=DEFAULT_CDP_PORT):
    if not require_runtime_dependencies("requests"):
        return False
    try:
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def check_login_state(cdp_port=DEFAULT_CDP_PORT):
    if not is_cdp_ready(cdp_port):
        return False
    try:
        ws = CDPSession(cdp_port)
        r = ws.send("Target.createTarget", {"url": "about:blank"})
        tid = r["result"]["targetId"]
        r = ws.send("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = r["result"]["sessionId"]

        ws.send("Page.navigate", {"url": "https://www.zhipin.com/"}, sid)
        time.sleep(3)

        val = ws.eval_js("document.cookie.includes('__zp_stoken__')", sid)
        is_logged_in = val == True

        ws.send("Target.closeTarget", {"targetId": tid})
        ws.close()
        return is_logged_in
    except Exception:
        return False


def run_setup_chrome(cdp_port=DEFAULT_CDP_PORT):
    os.makedirs(DEFAULT_CDP_DATA_DIR, exist_ok=True)
    print(f"准备启动 Chrome CDP 模式...")
    print(f"  CDP端口: {cdp_port}")
    print(f"  Profile目录: {DEFAULT_CDP_DATA_DIR}")

    cmd = [
        DEFAULT_CHROME_PATH,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={DEFAULT_CDP_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
    ]

    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)

    print(f"\n等待 Chrome 启动...")
    for i in range(30):
        time.sleep(1)
        if is_cdp_ready(cdp_port):
            print(f"✅ Chrome CDP 已就绪 (端口 {cdp_port})")
            print(f"\n请在弹出的 Chrome 浏览器中登录 BOSS直聘 (zhipin.com)")
            return
    print(f"❌ Chrome 启动超时")


def run_check(cdp_port=DEFAULT_CDP_PORT):
    print("=== 环境检查 ===")
    print()
    print("1. 检查依赖...")
    if require_runtime_dependencies("requests", "websocket"):
        print("   ✅ 依赖已安装")
    else:
        print("   ❌ 依赖缺失")
        return

    print()
    print("2. 检查 Chrome 路径...")
    if os.path.exists(DEFAULT_CHROME_PATH):
        print(f"   ✅ Chrome 路径: {DEFAULT_CHROME_PATH}")
    else:
        print(f"   ⚠️ Chrome 未找到: {DEFAULT_CHROME_PATH}")

    print()
    print("3. 检查 CDP 连接...")
    if is_cdp_ready(cdp_port):
        print(f"   ✅ CDP 已就绪 (端口 {cdp_port})")
        print()
        print("4. 检查登录状态...")
        if check_login_state(cdp_port):
            print("   ✅ 已登录 BOSS直聘")
        else:
            print("   ❌ 未登录 BOSS直聘")
    else:
        print(f"   ❌ CDP 未就绪 (端口 {cdp_port})")
        print(f"   请运行 --setup-chrome 启动 Chrome CDP")


# ============================================================
# main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description=f"BOSS直聘单职位详情抓取工具 (CDP版 - 纯DOM解析) v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 jd_cdp_parser.py --setup-chrome
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html"
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --debug
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --no-company
  python3 jd_cdp_parser.py --check
        """)
    parser.add_argument('url', nargs='?', help='BOSS直聘职位链接')
    parser.add_argument('--setup-chrome', action='store_true', help='启动 Chrome CDP 调试模式')
    parser.add_argument('--check', action='store_true', help='运行环境检查')
    parser.add_argument('--cdp-port', type=int, default=DEFAULT_CDP_PORT, help=f'CDP端口 (默认 {DEFAULT_CDP_PORT})')
    parser.add_argument('--debug', action='store_true', help='打印详细提取过程')
    parser.add_argument('--no-company', action='store_true', help='不抓取公司页面（仅 JD 页）')
    parser.add_argument('--jobs-dir', default='jobs', help='输出目录 (默认 jobs)')

    args = parser.parse_args()

    if args.check:
        run_check(args.cdp_port)
        return

    if args.setup_chrome:
        run_setup_chrome(args.cdp_port)
        return

    if not args.url:
        print("请输入职位链接:")
        args.url = input().strip()

    if not args.url.startswith('https://www.zhipin.com/'):
        print("错误：请输入有效的BOSS直聘链接")
        sys.exit(1)

    if not is_cdp_ready(args.cdp_port):
        print(f"❌ CDP 未就绪，请先运行 --setup-chrome 启动 Chrome")
        sys.exit(1)

    if not check_login_state(args.cdp_port):
        print("❌ 未检测到登录状态，请在 Chrome 中登录 zhipin.com")
        sys.exit(1)

    print(f"开始抓取: {args.url}")
    fetch_company = not args.no_company
    if fetch_company:
        print(f"  → 将同时抓取公司页面")
    else:
        print(f"  → 仅抓取 JD 页面")

    try:
        result = scrape_jd(args.url, args.cdp_port, debug=args.debug, fetch_company=fetch_company)
        structured = result.get('structured', {})

        job_dir, job_id = save_to_jobs_dir(result, args.jobs_dir)

        print(f"\n✅ 抓取成功！已保存到: {job_dir}")
        print(f"   ID: {job_id}")
        print(f"   职位: {structured.get('job_name', 'N/A')}")
        print(f"   公司: {structured.get('company_name', 'N/A')}")
        salary_low = structured.get('job_salary_low_10k')
        salary_high = structured.get('job_salary_high_10k')
        if salary_low and salary_high:
            print(f"   薪资: {salary_low}-{salary_high}万/年 {structured.get('salary_composition', '')}")
        print(f"   地址: {structured.get('company_address', 'N/A')}")
        print(f"   技术栈: {', '.join(structured.get('skill_tags', [])) or 'N/A'}")

        jd_len = len(structured.get('job_responsibility', '') + structured.get('job_requirement', ''))
        print(f"   JD 长度: {jd_len} 字")

        if result.get('company_data'):
            print(f"   公司页已抓取")
            print(f"   公司介绍: {len(structured.get('company_intro', ''))} 字")

        print(f"\n文件列表:")
        for f in os.listdir(job_dir):
            fpath = os.path.join(job_dir, f)
            size = os.path.getsize(fpath)
            print(f"   {f} ({size} bytes)")

        return
    except Exception as e:
        print(f"\n⚠️ 抓取失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
