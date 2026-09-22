from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import json
import re
import os
import sys

# ========== 路径处理（兼容源码 / exe）==========
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)              # exe 所在目录
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # .py 所在目录

# ========== 配置 ==========
URL = "https://ehall.xidian.edu.cn/"
SEARCH_KEYWORD = "个人方案查询"
WAIT_LOGIN_TIMEOUT = 300
OUTPUT_JSON = os.path.join(BASE_DIR, "培养方案.json")
OUTPUT_MD   = os.path.join(BASE_DIR, "培养方案.md")
SIDEBAR_WAIT = 1.5
# =========================

# ========== Debug 开关 ==========
DEBUG = True
DEBUG_DUMP_HTML = True
DEBUG_DUMP_KEYWORDS = ["体育"]
DEBUG_DUMP_DIR = os.path.join(BASE_DIR, "debug_html")
# ==============================

if DEBUG_DUMP_HTML and not os.path.exists(DEBUG_DUMP_DIR):
    os.makedirs(DEBUG_DUMP_DIR, exist_ok=True)


def log(*a):
    if DEBUG:
        print(*a)


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
    print("🌐 请手动完成登录（含验证码）...")
    deadline = time.time() + WAIT_LOGIN_TIMEOUT
    while time.time() < deadline:
        el = find_visible(driver, "input.search__content")
        if el:
            print("✅ 登录成功")
            return el
        time.sleep(1)
    raise TimeoutError("等待登录超时")


def do_search(driver, search_input, keyword):
    set_input_value(driver, search_input, keyword)
    time.sleep(0.5)
    btn = find_visible(driver, "button.search__action")
    if btn:
        js_click(driver, btn)
    else:
        search_input.send_keys(Keys.ENTER)
    print(f"🔍 已搜索：{keyword}")


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
    print("👆 已点击『进入应用』")
    time.sleep(3)
    new_tabs = set(driver.window_handles) - handles_before
    if new_tabs:
        driver.switch_to.window(new_tabs.pop())
        print("🆕 已切换新标签:", driver.title)


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
        print(f"📋 目标培养方案：{name}")
    except Exception:
        pass
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
    time.sleep(0.4)
    js_click(driver, card)
    print("👆 已点击培养方案卡片")


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
    print("⏳ 等待培养方案页面加载…")
    driver.switch_to.default_content()
    deadline = time.time() + timeout
    while time.time() < deadline:
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
        print("❌ 提取失败：", result.get("error"))
        return None
    print(f"✅ jsMind 实例：{result.get('foundName')}")
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
        html = driver.execute_script("return document.body.outerHTML;")
        safe_name = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", node_name)[:40]
        path = os.path.join(DEBUG_DUMP_DIR, f"{safe_name}_{node_id}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"    💾 已 dump 侧边栏 HTML: {path}")
    except Exception as e:
        log(f"    ⚠️ dump 失败: {e}")


def fetch_courses_from_sidebar(driver, node_id, node_name="", dump=False):
    """
    只收 kzh == node_id 的课程行。
    - kzh != node_id：侧边栏没刷新到当前节点，跳过
    - kzh 缺失：无法确认归属，跳过（避免幽灵行）
    """
    sel, rows = _find_course_rows(driver)

    if DEBUG:
        log(f"    选择器命中：{sel}，共 {len(rows)} 行")

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
                log(f"      [{i}] SKIP(no kzh) name={c.get('name')!r}")
            continue

        if kzh != node_id:
            skip_mismatch += 1
            if DEBUG:
                log(f"      [{i}] SKIP(kzh≠node) name={c.get('name')!r}")
            continue

        if c.get("code") or c.get("name"):
            courses.append(c)
            if DEBUG:
                log(f"      [{i}] KEEP name={c.get('name')!r}")

    if DEBUG and (skip_mismatch or skip_nokzh):
        log(f"    统计：KEEP={len(courses)} "
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
        log(f"    ⚠️ API select 失败：{result.get('error')}，回退物理点击")
        if not click_jmnode(driver, node_id):
            return False
    else:
        log(f"    ✅ API select_node OK (jm={result.get('foundName')})")

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: row_signature() != before
        )
        return True
    except Exception:
        log(f"    ⏱ 侧边栏指纹未变化（after={row_signature()}）")
        return True


def enrich_tree_with_courses(driver, tree):
    stats = {"nodes": 0, "nodes_with_courses": 0, "total_courses": 0}

    def walk(node, depth=0, is_root=False):
        node_id = node.get("id")
        node_name = node.get("name", "?")
        indent = "  " * depth

        stats["nodes"] += 1

        if is_root:
            print(f"{indent}🌱 {node_name}  [根节点，跳过]")
        else:
            hit_kw = any(kw in (node_name or "") for kw in DEBUG_DUMP_KEYWORDS)
            clicked = click_and_wait_sidebar(driver, node_id)
            if not clicked:
                print(f"{indent}⚠️ {node_name} 选中失败 (id={node_id})")
            else:
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
                    print(f"{indent}📚 {node_name}: {len(courses)} 门  "
                          f"[{names}{more}]")
                else:
                    print(f"{indent}· {node_name}")

        for c in node.get("children", []) or []:
            walk(c, depth + 1, is_root=False)

    walk(tree, depth=0, is_root=True)

    # 三保险：无论如何，根节点一定不带 courses
    tree.pop("courses", None)

    print(f"\n📈 遍历完成：节点 {stats['nodes']} 个，"
          f"有课程节点 {stats['nodes_with_courses']} 个，"
          f"课程总数 {stats['total_courses']}")
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


def print_tree(node, indent=0):
    prefix = "  " * indent
    score = node.get("score")
    score_str = f"（要求学分：{score}）" if score is not None else ""
    n = len(node.get("courses", []) or [])
    c_str = f"  [{n}门]" if n else ""
    print(f"{prefix}- {node.get('name','')}{score_str}{c_str}")
    for c in node.get("children", []) or []:
        print_tree(c, indent + 1)


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
# 主流程
# ------------------------------------------------------------
def main():
    options = Options()
    options.add_argument("--start-maximized")

    driver = webdriver.Edge(options=options)

    try:
        driver.get(URL)
        search_input = wait_for_login(driver)
        do_search(driver, search_input, SEARCH_KEYWORD)
        click_enter_app(driver, SEARCH_KEYWORD)
        click_plan_card(driver)

        raw_tree = extract_plan_tree(driver)
        if not raw_tree:
            print("❌ 未提取到树")
            input("按回车关闭...")
            return

        prepare_tree_names(raw_tree)

        if DEBUG:
            print("\n🌲 节点清单：")
            def _list(n, d=0):
                print(f"  {'  '*d}- id={n.get('id')!r}  name={n.get('name')!r}")
                for c in n.get("children", []) or []:
                    _list(c, d+1)
            _list(raw_tree)

        print("\n🔄 开始遍历所有节点抓取课程...\n")
        enrich_tree_with_courses(driver, raw_tree)

        final_tree = clean_tree(raw_tree, is_root=True)
        final_tree.pop("courses", None)

        print("\n📊 培养方案结构：\n")
        print_tree(final_tree)

        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(final_tree, f, ensure_ascii=False, indent=2)
        print(f"\n💾 已保存 JSON：{OUTPUT_JSON}")

        json_to_md(final_tree, OUTPUT_MD)
        print(f"📝 已保存 Markdown：{OUTPUT_MD}")

        def count_courses(n):
            return len(n.get("courses", []) or []) + sum(
                count_courses(c) for c in n.get("children", []) or []
            )
        print(f"ℹ️  课程总数：{count_courses(final_tree)}")

        if DEBUG_DUMP_HTML:
            print(f"🧪 命中关键词 [{', '.join(DEBUG_DUMP_KEYWORDS)}] 的侧边栏 HTML "
                  f"已保存到：{DEBUG_DUMP_DIR}/")

        input("\n按回车关闭浏览器...")

    except Exception as e:
        print(f"❌ 出错: {e}")
        try:
            driver.save_screenshot(os.path.join(BASE_DIR, "error.png"))
        except Exception:
            pass
        input("按回车关闭浏览器...")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()