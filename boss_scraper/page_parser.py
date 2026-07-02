"""
页面解析模块

包含:
  - EXTRACT_JD_PAGE_JS:      职位详情页 DOM 提取 JavaScript
  - EXTRACT_COMPANY_PAGE_JS: 公司介绍页 DOM 提取 JavaScript
  - parse_salary():          薪资文本解析
  - split_jd_description():  JD 全文拆分 (职责/要求)
  - build_structured():      构建结构化输出
  - scrape_jd_page():        抓取 JD 详情页
  - scrape_company_page():   抓取公司页
  - safe_json_loads():       安全 JSON 解析
"""

import json
import random
import time
import re

from .logger import get_logger
from .cdp_session import human_simulate

log = get_logger("parser")


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
    var compMatch = result.job_salary.match(/[·]\\s*(\\d+薪|期权|股票|股权)/);
    if (compMatch && !salaryComp) {
        salaryComp = compMatch[1];
    }
    result.salary_composition = salaryComp;

    // 公司页链接: 优先取带 ka 属性的具体公司链接
    var companyLink = '';
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
    if (!companyLink) {
        var gongsiLinks = document.querySelectorAll('a[href*="/gongsi/"]');
        for (var i = 0; i < gongsiLinks.length; i++) {
            var href = gongsiLinks[i].href || '';
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
# 工具函数
# ============================================================
def safe_json_loads(val, default=None):
    """安全解析 JSON 字符串"""
    if default is None:
        default = {}
    try:
        return json.loads(val) if isinstance(val, str) else default
    except (json.JSONDecodeError, ValueError, TypeError):
        return default


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
        raw = raw[: comp_match.start()].strip()

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
    responsibility = responsibility.replace('岗位职责：', '').replace('岗位职责:', '').strip()
    requirement = requirement.strip()

    return responsibility, requirement


# ============================================================
# 构建结构化输出
# ============================================================
def build_structured(jd_data, company_data=None):
    """从 JD 页和公司页 DOM 数据构建最终结构化 JSON

    Args:
        jd_data: 从 EXTRACT_JD_PAGE_JS 提取的数据
        company_data: 从 EXTRACT_COMPANY_PAGE_JS 提取的数据 (可选)

    Returns:
        对齐 references/jd_fields.md 的结构化字典
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
# 页面抓取函数
# ============================================================
def scrape_jd_page(ws, sid, url, debug=False):
    """加载 JD 详情页，纯 DOM 解析提取数据

    Args:
        ws: CDPSession 实例
        sid: Target sessionId
        url: JD 详情页 URL
        debug: 是否打印详细提取过程

    Returns:
        JD 页 DOM 提取数据字典
    """
    log.info(f"[JD页] 加载: {url}")
    ws.send("Page.navigate", {"url": url}, sid)
    time.sleep(random.uniform(5, 10))

    log.info("[JD页] 模拟阅读...")
    human_simulate(ws, sid)

    log.info("[JD页] DOM 解析提取...")
    raw_text = ws.eval_js(EXTRACT_JD_PAGE_JS, sid)
    jd_data = safe_json_loads(raw_text)

    if debug:
        log.debug("=" * 60)
        log.debug("JD 页 DOM 提取数据:")
        for k, v in jd_data.items():
            v_str = str(v)
            if len(v_str) > 150:
                v_str = v_str[:150] + "..."
            log.debug(f"  {k}: {v_str}")
        log.debug("=" * 60)

    return jd_data


def scrape_company_page(ws, sid, url, debug=False):
    """加载公司介绍页，纯 DOM 解析提取公司介绍、工作时间、经营范围

    Args:
        ws: CDPSession 实例
        sid: Target sessionId
        url: 公司介绍页 URL
        debug: 是否打印详细提取过程

    Returns:
        公司页 DOM 提取数据字典
    """
    log.info(f"[公司页] 加载: {url}")
    ws.send("Page.navigate", {"url": url}, sid)
    time.sleep(random.uniform(4, 8))

    log.info("[公司页] 模拟阅读...")
    human_simulate(ws, sid)

    log.info("[公司页] DOM 解析提取...")
    raw_text = ws.eval_js(EXTRACT_COMPANY_PAGE_JS, sid)
    company_data = safe_json_loads(raw_text)

    if debug:
        log.debug("=" * 60)
        log.debug("公司页 DOM 提取数据:")
        for k, v in company_data.items():
            v_str = str(v)
            if len(v_str) > 150:
                v_str = v_str[:150] + "..."
            log.debug(f"  {k}: {v_str}")
        log.debug("=" * 60)

    return company_data
