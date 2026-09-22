# -*- coding: utf-8 -*-
"""
基于 Selenium 的培养方案抓取工具（PySide6 界面版）

流程：登录 ehall → 搜索应用 → 打开「个人方案查询」→ 提取 jsMind 思维导图
      → 逐个选中节点抓取侧边栏课程 → 清洗 → 导出 JSON / Markdown
"""

import html
import json
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# ========== 路径处理（兼容源码 / exe）==========
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)              # exe 所在目录
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # .py 所在目录

# ========== 配置（要改就改这里，界面上不暴露）==========
DEFAULT_CONFIG = {
    "url": "https://ehall.xidian.edu.cn/",
    "keyword": "个人方案查询",
    "login_timeout": 300,        # 等待手动登录的秒数
    "output_dir": BASE_DIR,      # 输出目录，默认脚本 / exe 所在目录
}

CFG = SimpleNamespace(**DEFAULT_CONFIG)
# ======================================================

# 英语班型：界面显示名 → 培养方案里的班型节点名
# （学校官方叫「普通班」，界面上按大家习惯写成「初级」）
ENGLISH_LEVELS = {
    "初级": "英语分级普通班",
    "中级": "英语分级中级班",
    "高级": "英语分级高级班",
}

# ========== Debug 开关 ==========
DEBUG = True
DEBUG_DUMP_HTML = True
DEBUG_DUMP_KEYWORDS = ["体育"]
DEBUG_DUMP_DIR = os.path.join(BASE_DIR, "debug_html")
# ==============================

if DEBUG_DUMP_HTML and not os.path.exists(DEBUG_DUMP_DIR):
    os.makedirs(DEBUG_DUMP_DIR, exist_ok=True)


# ------------------------------------------------------------
# 消息上报层（界面 / 控制台共用）
# ------------------------------------------------------------
def _safe_print(msg):
    """打包成无控制台窗口的 exe 时 stdout 为 None，这里做兜底"""
    if sys.stdout is None:
        return
    try:
        print(msg)
    except Exception:
        pass


class ConsoleReporter:
    """默认上报器：直接把消息打到控制台"""

    def log(self, msg, level="info"):
        _safe_print(msg)

    def progress(self, done, total, text=""):
        pass

    def stage(self, text):
        _safe_print(text)


REPORTER = ConsoleReporter()


def emit(msg, level="info"):
    REPORTER.log(msg, level)


def emit_progress(done, total, text=""):
    REPORTER.progress(done, total, text)


def emit_stage(text):
    REPORTER.stage(text)


def dbg(msg):
    """调试日志（受 DEBUG 开关控制）"""
    if DEBUG:
        emit(msg, "debug")


# ------------------------------------------------------------
# 停止控制
# ------------------------------------------------------------
class StopRequested(Exception):
    """用户主动停止任务"""


_STOP_EVENT = threading.Event()
_ACTIVE_DRIVER = None


def set_active_driver(driver):
    global _ACTIVE_DRIVER
    _ACTIVE_DRIVER = driver


def check_stop():
    if _STOP_EVENT.is_set():
        raise StopRequested()


def _force_quit(driver):
    """停止任务时强关浏览器，让阻塞中的 Selenium 调用尽快失败"""
    try:
        driver.quit()
    except Exception:
        pass


# ------------------------------------------------------------
# 通用工具
# ------------------------------------------------------------
def find_visible(driver, css_selector, root=None):
    root = root or driver
    for el in root.find_elements(By.CSS_SELECTOR, css_selector):
        try:
            if el.is_displayed() and el.size["width"] > 0:
                return el
        except Exception:
            continue
    return None


def set_input_value(driver, element, value):
    driver.execute_script("""
        const el = arguments[0];
        const v  = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    """, element, value)


def js_click(driver, element):
    try:
        element.click()
        return True
    except Exception:
        driver.execute_script("""
            const el = arguments[0];
            el.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, view: window
            }));
        """, element)
        return False


def switch_to_iframe_with(driver, css_selector, max_depth=4):
    def dfs(depth):
        if driver.find_elements(By.CSS_SELECTOR, css_selector):
            return True
        if depth >= max_depth:
            return False
        for f in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            try:
                driver.switch_to.frame(f)
                if dfs(depth + 1):
                    return True
                driver.switch_to.parent_frame()
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    driver.switch_to.default_content()
        return False
    if dfs(0):
        return True
    driver.switch_to.default_content()
    return False


# ------------------------------------------------------------
# 步骤 1-4
# ------------------------------------------------------------
def wait_for_login(driver):
    emit_stage("等待登录")
    emit("🌐 请在弹出的浏览器里手动完成登录（含验证码），程序会自动检测…", "info")
    deadline = time.time() + CFG.login_timeout
    while time.time() < deadline:
        check_stop()
        el = find_visible(driver, "input.search__content")
        if el:
            emit("✅ 登录成功", "success")
            return el
        time.sleep(1)
    raise TimeoutError(f"等待登录超时（当前设定 {CFG.login_timeout} 秒，可在界面上调大）")


def do_search(driver, search_input, keyword):
    set_input_value(driver, search_input, keyword)
    time.sleep(0.5)
    btn = find_visible(driver, "button.search__action")
    if btn:
        js_click(driver, btn)
    else:
        search_input.send_keys(Keys.ENTER)
    emit(f"🔍 已搜索：{keyword}", "info")


def click_enter_app(driver, app_name):
    handles_before = set(driver.window_handles)
    xpath = (
        f"//li[contains(@class,'appitem')]"
        f"[.//div[contains(@class,'appitem__name') "
        f"      and normalize-space(text())='{app_name}']]"
        f"//button[normalize-space(text())='进入应用']"
    )
    btn = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.XPATH, xpath))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    time.sleep(0.3)
    js_click(driver, btn)
    emit("👆 已点击『进入应用』", "info")
    time.sleep(3)
    new_tabs = set(driver.window_handles) - handles_before
    if new_tabs:
        driver.switch_to.window(new_tabs.pop())
        emit(f"🆕 已切换新标签：{driver.title}", "info")


def click_plan_card(driver):
    CSS_CARD = 'div.grpyfa-content[data-action="详情"]'
    time.sleep(2)
    driver.switch_to.default_content()
    if not driver.find_elements(By.CSS_SELECTOR, CSS_CARD):
        switch_to_iframe_with(driver, CSS_CARD, max_depth=4)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, CSS_CARD))
    )
    card = driver.find_element(By.CSS_SELECTOR, CSS_CARD)
    try:
        name = card.find_element(By.CSS_SELECTOR, ".grpyfa-top-name").text
        emit(f"📋 目标培养方案：{name}", "info")
    except Exception:
        pass
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
    time.sleep(0.4)
    js_click(driver, card)
    emit("👆 已点击培养方案卡片", "info")


# ------------------------------------------------------------
# 步骤 5：提取 jsMind 树
# ------------------------------------------------------------
JS_EXPORT_TREE = r"""
return (function() {
    try {
        var candidates = ['_jm', 'jm', 'jsMind', 'jsmind', 'myJsMind',
                          'jmInstance', 'jmn', 'mind', 'this_jm'];
        var jm = null, foundName = null;
        for (var i = 0; i < candidates.length; i++) {
            try {
                var v = window[candidates[i]];
                if (v && v.mind && v.mind.root) { jm = v; foundName = candidates[i]; break; }
            } catch (e) {}
        }
        if (!jm) {
            for (var k in window) {
                try {
                    var v = window[k];
                    if (v && typeof v === 'object' && v.mind && v.mind.root
                        && v.mind.root.topic !== undefined) {
                        jm = v; foundName = k; break;
                    }
                } catch (e) {}
            }
        }
        if (!jm) {
            var c = document.getElementById('jsmind_container');
            if (c) {
                var keys = Object.getOwnPropertyNames(c);
                for (var i2 = 0; i2 < keys.length; i2++) {
                    try {
                        var v2 = c[keys[i2]];
                        if (v2 && v2.mind && v2.mind.root) {
                            jm = v2; foundName = 'container.' + keys[i2]; break;
                        }
                    } catch (e) {}
                }
            }
        }
        if (!jm) return JSON.stringify({ok:false, error:'jsMind instance not found'});

        function toObj(node) {
            var o = { id: node.id, topic: node.topic };
            if (node.children && node.children.length > 0) {
                o.children = node.children.map(toObj);
            }
            return o;
        }
        return JSON.stringify({ ok: true, foundName: foundName, data: toObj(jm.mind.root) });
    } catch (e) {
        return JSON.stringify({ ok: false, error: 'Exception: ' + e.message });
    }
})();
"""


# 通过 jsMind API 选中节点（触发侧边栏刷新）
JS_SELECT_NODE = r"""
return (function(nodeId) {
    try {
        var candidates = ['_jm', 'jm', 'jsMind', 'jsmind', 'myJsMind',
                          'jmInstance', 'jmn', 'mind', 'this_jm'];
        var jm = null, foundName = null;
        for (var i = 0; i < candidates.length; i++) {
            try {
                var v = window[candidates[i]];
                if (v && v.mind && v.mind.root && typeof v.select_node === 'function') {
                    jm = v; foundName = candidates[i]; break;
                }
            } catch (e) {}
        }
        if (!jm) {
            for (var k in window) {
                try {
                    var v = window[k];
                    if (v && typeof v === 'object' && v.mind && v.mind.root
                        && typeof v.select_node === 'function') {
                        jm = v; foundName = k; break;
                    }
                } catch (e) {}
            }
        }
        if (!jm) return JSON.stringify({ok:false, error:'jsMind not found'});

        try {
            var n = jm.get_node(nodeId);
            while (n) {
                try { jm.expand_node(n); } catch (e) {}
                n = n.parent;
            }
        } catch (e) {}

        try {
            jm.select_node(nodeId);
            return JSON.stringify({ok:true, foundName: foundName});
        } catch (e) {
            return JSON.stringify({ok:false, error:'select_node: ' + e.message});
        }
    } catch (e) {
        return JSON.stringify({ok:false, error:'Exception: ' + e.message});
    }
})(arguments[0]);
"""


def extract_plan_tree(driver, timeout=40):
    emit_stage("等待培养方案页面")
    emit("⏳ 等待培养方案页面加载…", "info")
    driver.switch_to.default_content()
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_stop()
        if driver.find_elements(By.CSS_SELECTOR, "jmnode"):
            break
        switch_to_iframe_with(driver, "jmnode", max_depth=4)
        if driver.find_elements(By.CSS_SELECTOR, "jmnode"):
            break
        time.sleep(1)
    if not driver.find_elements(By.CSS_SELECTOR, "jmnode"):
        return None
    time.sleep(2)
    raw = driver.execute_script(JS_EXPORT_TREE)
    if raw is None:
        return None
    result = raw if isinstance(raw, dict) else json.loads(raw)
    if not result.get("ok"):
        emit(f"❌ 提取失败：{result.get('error')}", "error")
        return None
    emit(f"✅ jsMind 实例：{result.get('foundName')}", "success")
    return result["data"]


# ------------------------------------------------------------
# 给节点补 name/score（保留 id 供后续点击）
# ------------------------------------------------------------
TITLE_RE = re.compile(r"""title=['"]([^'"]+)['"]""")
SCORE_RE = re.compile(r"要求学分[：:]\s*(\d+(?:\.\d+)?)")
NAME_RE  = re.compile(r">([^<]*?)</span>", re.S)
TAG_RE   = re.compile(r"<[^>]+>")


def parse_topic(topic_html):
    if not topic_html:
        return "", None
    name = ""
    m = TITLE_RE.search(topic_html)
    if m:
        name = m.group(1).strip()
    else:
        m = NAME_RE.search(topic_html)
        if m:
            name = m.group(1).strip()
        else:
            name = TAG_RE.sub("", topic_html).strip()
    score = None
    ms = SCORE_RE.search(topic_html)
    if ms:
        v = float(ms.group(1))
        score = int(v) if v.is_integer() else v
    return name, score


def prepare_tree_names(node):
    name, score = parse_topic(node.get("topic", ""))
    node["name"] = name
    node["score"] = score
    for c in node.get("children", []) or []:
        prepare_tree_names(c)


# ------------------------------------------------------------
# 步骤 6：遍历节点抓课程
# ------------------------------------------------------------
def click_jmnode(driver, node_id):
    """物理点击（API 失败时的回退方案）"""
    selector = f'jmnode[nodeid="{node_id}"]'
    try:
        jmnode = driver.find_element(By.CSS_SELECTOR, selector)
    except Exception:
        return False

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", jmnode)
    time.sleep(0.2)

    spans = jmnode.find_elements(By.CSS_SELECTOR, "span.jsmind_node_title_span")
    target = spans[0] if spans else jmnode

    try:
        target.click()
        return True
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", target)
        return True
    except Exception:
        pass
    try:
        driver.execute_script("""
            const el = arguments[0];
            el.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, view: window
            }));
        """, target)
        return True
    except Exception:
        return False


# 候选课程行选择器
COURSE_ROW_SELECTORS = [
    "tr.jsmind-course-tr",
    "tr.jsmind-course-row",
    "li.jsmind-course-item",
    "div.jsmind-course-row",
    "div.jsmind-course-item",
    ".jsmind-course-list > *",
]


def parse_course_row(row):
    course = {}

    try:
        code_el = row.find_element(
            By.CSS_SELECTOR, "span[title][style*='font-weight']"
        )
        course["code"] = code_el.get_attribute("title") or code_el.text.strip()
    except Exception:
        pass

    try:
        name_el = row.find_element(By.CSS_SELECTOR, "span.jsmind-corse-name")
        course["name"] = name_el.get_attribute("title") or name_el.text.strip()
    except Exception:
        course["name"] = ""

    for lb in row.find_elements(By.CSS_SELECTOR, "label"):
        t = (lb.text or "").strip()
        if not t:
            continue
        m = re.search(r"学分[：:]\s*([\d.]+)", t)
        if m:
            v = float(m.group(1))
            course["credit"] = int(v) if v.is_integer() else v
            continue
        m = re.search(r"学期[：:]\s*([^】]+)", t)
        if m:
            course["semester"] = m.group(1).strip()
            continue
        if t in ("【考试】", "【考查】"):
            course["exam_type"] = t.strip("【】")
            continue
        if t in ("【必修】", "【限选】", "【任选】", "【选修】"):
            course["category"] = t.strip("【】")
            continue
        if "方向" in t:
            course["direction"] = t.strip("【】")
            continue
        if "学院" in t:
            course["college"] = t.strip("【】")
            continue

    try:
        edit = row.find_element(By.CSS_SELECTOR, "a.jsmind-course-edit")
        course["kch"] = edit.get_attribute("kch")
        course["kzh"] = edit.get_attribute("kzh")
    except Exception:
        pass

    return course


def _find_course_rows(driver):
    """返回 (selector_used, [row_elements])"""
    for sel in COURSE_ROW_SELECTORS:
        rows = driver.find_elements(By.CSS_SELECTOR, sel)
        if rows:
            return sel, rows
    return None, []


def _dump_sidebar_html(driver, node_id, node_name):
    if not DEBUG_DUMP_HTML:
        return
    try:
        html_text = driver.execute_script("return document.body.outerHTML;")
        safe_name = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", node_name)[:40]
        path = os.path.join(DEBUG_DUMP_DIR, f"{safe_name}_{node_id}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_text)
        dbg(f"    💾 已 dump 侧边栏 HTML: {path}")
    except Exception as e:
        dbg(f"    ⚠️ dump 失败: {e}")


def fetch_courses_from_sidebar(driver, node_id, node_name="", dump=False):
    """
    只收 kzh == node_id 的课程行。
    - kzh != node_id：侧边栏没刷新到当前节点，跳过
    - kzh 缺失：无法确认归属，跳过（避免幽灵行）
    """
    sel, rows = _find_course_rows(driver)

    if DEBUG:
        dbg(f"    选择器命中：{sel}，共 {len(rows)} 行")

    if not rows and dump:
        _dump_sidebar_html(driver, node_id, node_name)

    courses = []
    skip_mismatch = 0
    skip_nokzh = 0

    for i, row in enumerate(rows):
        c = parse_course_row(row)
        kzh = c.get("kzh")

        if not kzh:
            skip_nokzh += 1
            if DEBUG:
                dbg(f"      [{i}] SKIP(no kzh) name={c.get('name')!r}")
            continue

        if kzh != node_id:
            skip_mismatch += 1
            if DEBUG:
                dbg(f"      [{i}] SKIP(kzh≠node) name={c.get('name')!r}")
            continue

        if c.get("code") or c.get("name"):
            courses.append(c)
            if DEBUG:
                dbg(f"      [{i}] KEEP name={c.get('name')!r}")

    if DEBUG and (skip_mismatch or skip_nokzh):
        dbg(f"    统计：KEEP={len(courses)} "
            f"SKIP_mismatch={skip_mismatch} SKIP_nokzh={skip_nokzh}")

    return courses


def click_and_wait_sidebar(driver, node_id, timeout=6):
    """用 jsMind API 选中节点，等侧边栏刷新到新内容"""

    def row_signature():
        sel, rows = _find_course_rows(driver)
        if not rows:
            return (0, "")
        parts = []
        for r in rows[:5]:
            try:
                parts.append((r.text or "")[:30])
            except Exception:
                parts.append("")
        return (len(rows), "|".join(parts))

    before = row_signature()

    raw = driver.execute_script(JS_SELECT_NODE, node_id)
    result = raw if isinstance(raw, dict) else json.loads(raw)

    if not result.get("ok"):
        dbg(f"    ⚠️ API select 失败：{result.get('error')}，回退物理点击")
        if not click_jmnode(driver, node_id):
            return False
    else:
        dbg(f"    ✅ API select_node OK (jm={result.get('foundName')})")

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: row_signature() != before
        )
        return True
    except Exception:
        dbg(f"    ⏱ 侧边栏指纹未变化（after={row_signature()}）")
        return True


def enrich_tree_with_courses(driver, tree):
    """按 DFS 前序逐个节点抓取课程，并向上汇报进度"""
    items = []

    def collect(node, depth, is_root):
        items.append((node, depth, is_root))
        for c in node.get("children", []) or []:
            collect(c, depth + 1, False)

    collect(tree, 0, True)

    total = max(len(items) - 1, 0)     # 根节点不抓课程
    stats = {"nodes": 0, "nodes_with_courses": 0, "total_courses": 0}
    done = 0

    for node, depth, is_root in items:
        check_stop()

        node_id = node.get("id")
        node_name = node.get("name", "?")
        indent = "  " * depth
        stats["nodes"] += 1

        if is_root:
            dbg(f"{indent}🌱 {node_name}  [根节点，跳过]")
            continue

        done += 1
        emit_progress(done, total, node_name)

        hit_kw = any(kw in (node_name or "") for kw in DEBUG_DUMP_KEYWORDS)
        clicked = click_and_wait_sidebar(driver, node_id)
        if not clicked:
            emit(f"{indent}⚠️ [{done}/{total}] {node_name} 选中失败 (id={node_id})", "warn")
            continue

        courses = fetch_courses_from_sidebar(
            driver, node_id, node_name=node_name, dump=hit_kw
        )
        if courses:
            node["courses"] = courses
            stats["nodes_with_courses"] += 1
            stats["total_courses"] += len(courses)
            names = ", ".join(
                (c.get("name") or c.get("code") or "?")
                for c in courses[:3]
            )
            more = " ..." if len(courses) > 3 else ""
            emit(f"{indent}📚 [{done}/{total}] {node_name}: {len(courses)} 门  "
                 f"[{names}{more}]", "success")
        else:
            emit(f"{indent}· [{done}/{total}] {node_name}")

    # 三保险：无论如何，根节点一定不带 courses
    tree.pop("courses", None)

    emit(f"📈 遍历完成：节点 {stats['nodes']} 个，"
         f"有课程节点 {stats['nodes_with_courses']} 个，"
         f"课程总数 {stats['total_courses']}", "success")
    return tree


# ------------------------------------------------------------
# 最终清洗（只保留 name / credit / semester）
# ------------------------------------------------------------
COURSE_KEEP = ("name", "credit", "semester")


def clean_course(c):
    return {k: c[k] for k in COURSE_KEEP if k in c and c[k] not in (None, "")}


def clean_tree(node, is_root=False):
    """根节点不输出 courses（is_root=True）"""
    out = {"name": node.get("name", ""), "score": node.get("score")}

    if not is_root and "courses" in node:
        out["courses"] = [clean_course(c) for c in node["courses"]]

    if node.get("children"):
        out["children"] = [clean_tree(c, is_root=False) for c in node["children"]]

    return out


def count_courses(node):
    return len(node.get("courses", []) or []) + sum(
        count_courses(c) for c in node.get("children", []) or []
    )


# ------------------------------------------------------------
# 英语班型过滤
# ------------------------------------------------------------
def filter_english_level(tree, level):
    """
    只保留所选班型，其余班型整块删掉。

    「外语类课程」下面并列着三个节点：
        英语分级普通班 / 英语分级中级班 / 英语分级高级班
    每个班型底下各挂着一份内容完全相同的「高级英语选修课程（2024）」，
    不筛的话导出的 MD 里会出现三份重复的选修课清单。
    """
    keep_name = ENGLISH_LEVELS.get(level)
    if not keep_name:
        emit(f"⚠️ 未知的英语班型「{level}」，跳过筛选", "warn")
        return tree

    kept, removed = [], []

    def walk(node):
        children = node.get("children")
        if not children:
            return
        survivors = []
        for child in children:
            name = child.get("name", "")
            if name in ENGLISH_LEVELS.values():
                if name != keep_name:
                    removed.append(name)
                    continue          # 不入 survivors，等于整块删掉
                kept.append(name)
            walk(child)
            survivors.append(child)
        node["children"] = survivors

    walk(tree)

    if not kept:
        emit(f"⚠️ 树里没找到「{keep_name}」，英语班型筛选未生效", "warn")
    elif removed:
        emit(f"🔤 英语班型「{level}」：保留「{keep_name}」，"
             f"移除 {'、'.join(removed)}", "info")
    else:
        emit(f"🔤 英语班型「{level}」：只有「{keep_name}」，无需移除", "info")

    return tree


def show_tree(node, indent=0):
    """把培养方案结构打到调试日志里"""
    if not DEBUG:
        return
    prefix = "  " * indent
    score = node.get("score")
    score_str = f"（要求学分：{score}）" if score is not None else ""
    n = len(node.get("courses", []) or [])
    c_str = f"  [{n}门]" if n else ""
    dbg(f"{prefix}- {node.get('name','')}{score_str}{c_str}")
    for c in node.get("children", []) or []:
        show_tree(c, indent + 1)


# ------------------------------------------------------------
# 树 → Markdown
# ------------------------------------------------------------
def json_to_md(tree, output_path):
    """
    把培养方案树翻译成 Markdown。

    规则：
      - 节点深度 d → Markdown 标题级别 (d+1)，最多 6 级
      - 标题格式：`# 名称 总学分：X`
      - 课程格式：`- [ ] 课程名 X学分 学期`
    """
    lines = []

    def render(node, depth):
        name = node.get("name", "") or ""
        score = node.get("score")

        level = min(depth + 1, 6)
        header = "#" * level

        if score is not None:
            title = f"{name} 总学分：{score}"
        else:
            title = name
        lines.append(f"{header} {title}")
        lines.append("")

        for c in node.get("courses", []) or []:
            parts = [f"- [ ] {c.get('name', '')}"]
            credit = c.get("credit")
            if credit is not None:
                parts.append(f"{credit}学分")
            semester = c.get("semester")
            if semester:
                parts.append(semester)
            lines.append(" ".join(parts))

        if node.get("courses"):
            lines.append("")

        for child in node.get("children", []) or []:
            render(child, depth + 1)

    render(tree, 0)

    # 折叠连续空行
    out = []
    prev_blank = False
    for ln in lines:
        if ln == "":
            if not prev_blank:
                out.append(ln)
            prev_blank = True
        else:
            out.append(ln)
            prev_blank = False

    text = "\n".join(out).rstrip() + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return output_path


# ------------------------------------------------------------
# 抓取主流程（在后台线程里跑）
# ------------------------------------------------------------
def run_pipeline(english_level=None):
    """完整抓取流程；成功返回 (True, 摘要)，失败直接抛异常"""
    out_json = os.path.join(CFG.output_dir, "培养方案.json")
    out_md = os.path.join(CFG.output_dir, "培养方案.md")

    emit_stage("启动浏览器")
    emit("🚀 启动 Edge 浏览器…", "info")

    options = Options()
    options.add_argument("--start-maximized")
    driver = webdriver.Edge(options=options)
    set_active_driver(driver)

    try:
        check_stop()
        driver.get(CFG.url)

        search_input = wait_for_login(driver)
        check_stop()

        do_search(driver, search_input, CFG.keyword)
        click_enter_app(driver, CFG.keyword)
        click_plan_card(driver)

        raw_tree = extract_plan_tree(driver)
        if not raw_tree:
            raise RuntimeError("未提取到培养方案树（页面结构可能变了，或加载超时）")

        prepare_tree_names(raw_tree)

        # 先筛掉不要的班型，省得白点那几个节点
        filter_english_level(raw_tree, english_level)

        if DEBUG:
            emit("🌲 节点清单：", "debug")
            show_tree(raw_tree)

        emit_stage("抓取课程")
        emit("🔄 开始遍历所有节点抓取课程…", "info")
        enrich_tree_with_courses(driver, raw_tree)

        check_stop()

        final_tree = clean_tree(raw_tree, is_root=True)
        final_tree.pop("courses", None)

        show_tree(final_tree)

        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(final_tree, f, ensure_ascii=False, indent=2)
        emit(f"💾 已保存 JSON：{out_json}", "success")

        json_to_md(final_tree, out_md)
        emit(f"📝 已保存 Markdown：{out_md}", "success")

        total = count_courses(final_tree)
        emit(f"ℹ️  课程总数：{total}", "success")

        if DEBUG_DUMP_HTML:
            emit(f"🧪 命中关键词 [{', '.join(DEBUG_DUMP_KEYWORDS)}] 的侧边栏 HTML "
                 f"已保存到：{DEBUG_DUMP_DIR}", "debug")

        emit_stage("完成")
        return True, f"抓取完成：共 {total} 门课程，已导出到 {CFG.output_dir}"

    except StopRequested:
        raise
    except Exception:
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "error.png"))
            emit("📸 已保存出错截图 error.png", "warn")
        except Exception:
            pass
        raise
    finally:
        set_active_driver(None)
        try:
            driver.quit()
        except Exception:
            pass


# ============================================================
# 界面
# ============================================================
LOG_COLORS = {
    "info": "#c9cdd8",
    "success": "#3ecf8e",
    "warn": "#f5a623",
    "error": "#f0616d",
    "debug": "#6b7180",
}

QSS = """
QWidget#Root { background-color: #16171d; }

QLabel { color: #c9cdd8; font-size: 13px; }
QLabel#Title { font-size: 21px; font-weight: 600; color: #f2f4fa; }
QLabel#Subtitle { font-size: 12px; color: #7d8496; }
QLabel#FieldLabel { font-size: 13px; color: #9aa3b8; }
QLabel#Status { font-size: 13px; color: #8ea2c8; }

QComboBox {
    background-color: #14161c;
    border: 1px solid #2b2e3a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e6e8ee;
}
QComboBox:hover { border-color: #3a3f4d; }
QComboBox:focus { border-color: #4c8dff; }
QComboBox:disabled { color: #565b6b; background-color: #1c1e26; border-color: #262935; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    margin-right: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #8b90a0;
}
QComboBox QAbstractItemView {
    background-color: #1e2029;
    border: 1px solid #2b2e3a;
    border-radius: 6px;
    color: #e6e8ee;
    outline: none;
    padding: 4px;
    selection-background-color: #3b6fd4;
}

QPushButton {
    background-color: #262935;
    border: 1px solid #333742;
    border-radius: 6px;
    padding: 8px 18px;
    color: #d5d9e3;
    font-size: 13px;
}
QPushButton:hover { background-color: #2f3342; }
QPushButton:pressed { background-color: #21242f; }
QPushButton:disabled { color: #565b6b; background-color: #1c1e26; border-color: #262935; }

QPushButton#Primary {
    background-color: #3b6fd4;
    border: 1px solid #3b6fd4;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#Primary:hover { background-color: #4a7ee2; }
QPushButton#Primary:pressed { background-color: #3363c2; }
QPushButton#Primary:disabled {
    background-color: #2a3550;
    border-color: #2a3550;
    color: #77809a;
}

QProgressBar {
    background-color: #1b1d25;
    border: none;
    border-radius: 5px;
    min-height: 10px;
    max-height: 10px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #3b6fd4;
    border-radius: 5px;
}

QPlainTextEdit {
    background-color: #101218;
    border: 1px solid #242733;
    border-radius: 8px;
    padding: 8px;
    color: #c9cdd8;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #333742;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #3f4453; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #333742;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
"""


class ScrapeWorker(QThread):
    """后台抓取线程：通过信号把日志 / 进度回传界面"""

    sig_log = Signal(str, str)              # (文本, 级别)
    sig_progress = Signal(int, int, str)    # (已完成, 总数, 当前节点)
    sig_stage = Signal(str)                 # 阶段描述
    sig_finished = Signal(bool, str)        # (是否成功, 摘要)

    def __init__(self, english_level=None, parent=None):
        super().__init__(parent)
        self.english_level = english_level

    # ---- 上报接口（供抓取逻辑调用）----
    def log(self, msg, level="info"):
        self.sig_log.emit(str(msg), level)

    def progress(self, done, total, text=""):
        self.sig_progress.emit(int(done), int(total), str(text))

    def stage(self, text):
        self.sig_stage.emit(str(text))

    # ---- 停止 ----
    def request_stop(self):
        _STOP_EVENT.set()
        driver = _ACTIVE_DRIVER
        if driver is not None:
            threading.Thread(target=_force_quit, args=(driver,), daemon=True).start()

    def run(self):
        global REPORTER
        REPORTER = self
        try:
            ok, msg = run_pipeline(self.english_level)
            self.sig_finished.emit(ok, msg)
        except StopRequested:
            self.sig_finished.emit(False, "⏹ 已停止（未写入文件）")
        except Exception as e:
            if _STOP_EVENT.is_set():
                # 强关浏览器会让正在执行的 Selenium 调用报错，这属于正常停止
                self.sig_finished.emit(False, "⏹ 已停止（未写入文件）")
            else:
                self.sig_finished.emit(False, f"❌ 出错：{type(e).__name__}: {e}")
        finally:
            REPORTER = ConsoleReporter()


class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("培养方案抓取工具")
        self.resize(760, 700)
        self.setMinimumSize(620, 540)

        self.worker = None

        self._build_ui()
        self._bind()

    # ---------------- 界面搭建 ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        title = QLabel("培养方案 → Markdown")
        title.setObjectName("Title")
        subtitle = QLabel(
            "抓取过程会打开 Edge，请在里面手动完成登录（含验证码），然后等它自己跑完"
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        root.addLayout(self._build_action_row())
        root.addLayout(self._build_progress_row())
        root.addWidget(self._build_log_area(), 1)

    def _build_action_row(self):
        self.btn_start = QPushButton("开始抓取")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setMinimumHeight(36)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setEnabled(False)

        self.btn_open = QPushButton("打开输出目录")
        self.btn_open.setMinimumHeight(36)

        self.level_label = QLabel("英语班型")
        self.level_label.setObjectName("FieldLabel")

        self.level_combo = QComboBox()
        self.level_combo.addItems(list(ENGLISH_LEVELS))
        self.level_combo.setFixedWidth(96)
        self.level_combo.setMinimumHeight(36)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.level_label)
        row.addWidget(self.level_combo)
        row.addSpacing(4)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        row.addWidget(self.btn_open)
        return row

    def _build_progress_row(self):
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Status")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addWidget(self.status_label)
        col.addWidget(self.progress)
        return col

    def _build_log_area(self):
        label = QLabel("运行日志")
        label.setObjectName("FieldLabel")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)

        font = QFont()
        font.setFamilies(["Cascadia Mono", "Consolas", "Microsoft YaHei UI"])
        font.setPointSize(10)
        self.log_view.setFont(font)

        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(8)
        wrap.addWidget(label)
        wrap.addWidget(self.log_view, 1)

        box = QWidget()
        box.setLayout(wrap)
        return box

    def _bind(self):
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_open.clicked.connect(self.on_open_output)

    # ---------------- 日志 ----------------
    def append_log(self, text, level="info"):
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        stamp = time.strftime("%H:%M:%S")
        safe = html.escape(str(text)).replace("\n", "<br>")
        self.log_view.appendHtml(
            f'<span style="color:#545a6b">[{stamp}]</span> '
            f'<span style="color:{color}">{safe}</span>'
        )
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---------------- 交互 ----------------
    def _set_running(self, running):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.level_combo.setEnabled(not running)

    def on_open_output(self):
        target = CFG.output_dir
        if not os.path.isdir(target):
            QMessageBox.information(self, "提示", f"目录不存在：\n{target}")
            return
        try:
            os.startfile(target)          # Windows 专用
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def on_start(self):
        try:
            os.makedirs(CFG.output_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "输出目录无效", f"无法创建输出目录：\n{e}")
            return

        self.log_view.clear()
        self.progress.setRange(0, 0)          # 未确定进度
        self.status_label.setText("启动中…")
        self._set_running(True)
        self.append_log("🚀 任务开始", "info")

        _STOP_EVENT.clear()
        self.worker = ScrapeWorker(self.level_combo.currentText())
        self.worker.sig_log.connect(self.append_log)
        self.worker.sig_stage.connect(self.on_stage)
        self.worker.sig_progress.connect(self.on_progress)
        self.worker.sig_finished.connect(self.on_finished)
        self.worker.start()

    def on_stop(self):
        if self.worker and self.worker.isRunning():
            self.btn_stop.setEnabled(False)
            self.append_log("⏹ 正在停止，请稍候…", "warn")
            self.worker.request_stop()

    def on_stage(self, text):
        self.status_label.setText(text)

    def on_progress(self, done, total, text):
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        if self.progress.maximum() != total or self.progress.minimum() != 0:
            self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status_label.setText(f"抓取节点 {done}/{total}：{text}")

    def on_finished(self, ok, message):
        self._set_running(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if ok else 0)
        self.status_label.setText("完成" if ok else "已结束")
        self.append_log(message, "success" if ok else "warn")

    # ---------------- 关闭窗口 ----------------
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "确认退出", "抓取还在进行中，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(5000)
        event.accept()


# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("培养方案抓取")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
