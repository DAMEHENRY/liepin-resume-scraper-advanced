import asyncio
import os
import pandas as pd
import random
import json
import requests
from playwright.async_api import async_playwright
import threading
import time
import re # <-- 已导入 re
import pypinyin

# Constants
VOLC_SECRETKEY = "0c0d7998-f994-49c5-afb6-072755da3c89"
RESUME_LINK_SELECTOR = "div.new-resume-personal-name"  # Selector for clicking resumes on search page
CV_TEXT_SELECTOR = ".G0UQv"  # Selector for resume content

# --- [!!! 修改点 1: 全局变量 !!!] ---
# Global variables for pause functionality
pause_flag = threading.Event()
pause_flag.set()  # Initially running
# Global variables for thread-safe saving
contacts_lock = threading.Lock()
saved_contacts = []
output_filename = "" 
qualified_resumes_count = 0 # <-- 新增: n (合格数)
processed_resumes_count = 0 # <-- 新增: m (已看数)
# --- [!!! 修改结束 !!!] ---


async def save_session():
    """
    仅运行一次。
    运行此函数，在弹出的浏览器中手动登录猎聘网。
    登录成功后，按 Enter 键，会话将保存到 state.json。
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto("https://h.liepin.com/search/getConditionItem")
        print("--- 请在弹出的浏览器窗口中手动登录猎聘网 ---")
        print("--- 登录成功后，返回此终端，按 Enter 键继续 ---")
        input() # 脚本会暂停在这里，等你登录
        
        await context.storage_state(path="state.json")
        print("登录状态已保存到 state.json。")
        await browser.close()

# -------------------------------------------------------------------
# 2. 火山引擎 AI 决策函数 (使用 requests)
# -------------------------------------------------------------------

def is_match_volc(cv_text, briefing):
    """
    使用火山引擎REST API（通过 requests 库）判断简历是否匹配提纲。
    此方法绕过了 SDK 导入问题，直接调用 API 端点。
    """
    # Use the constant defined at the top of the file
    api_key = VOLC_SECRETKEY
    if not api_key:
        print("错误: 未找到 VOLC_SECRETKEY 常量。请确保已正确设置。")
        return False

    # <-- !!! [用户必须修改] !!! -->
    # 替换为你在火山方舟平台上选择的模型的 Endpoint ID
    # 例如："doubao-pro-32k", "doubao-pro-128k" 等
    MODEL_ENDPOINT_ID = "doubao-seed-1-6-lite-251015" # 示例模型
    API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

    prompt = f"""
    你是一个专业的招聘/访谈助手。你的任务是判断一份简历是否符合访谈提纲的要求。

    【访谈提纲】:
    {briefing}

    【候选人简历】:
    {cv_text}

    【你的任务】:
    请仔细阅读提纲和简历，判断该候选人是否符合提纲中的核心要求。
    
    请只回答 "YES" 或 "NO"。
    """
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "model": MODEL_ENDPOINT_ID,
        "max_completion_tokens": 65535,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "reasoning_effort": "medium"
    }

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status() # 如果请求失败 (例如 4xx, 5xx 错误), 则抛出异常
        
        result = response.json()
        
        if 'error' in result:
            print(f"火山引擎 API 返回错误: {result['error']['message']}")
            return False

        answer = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        answer = answer.strip().upper()

        if not answer:
            print("火山引擎 API 未返回有效答案。")
            return False
        
        print(f"--- 火山引擎 AI 判断结果: {answer} ---")
        return "YES" in answer

    except requests.exceptions.RequestException as e:
        print(f"火山引擎 API 请求出错: {e}")
        return False
    except Exception as e:
        print(f"处理火山引擎响应时出错: {e}")
        return False

def summarize_profile_volc(cv_text, target_company):
    """
    使用火山引擎AI总结简历，并按要求格式化。
    """
    api_key = VOLC_SECRETKEY
    if not api_key:
        return "错误: 未找到 VOLC_SECRETKEY。"

    MODEL_ENDPOINT_ID = "doubao-seed-1-6-lite-251015"
    API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

    prompt = f"""
你是一位专业的简历分析师。你的任务是严格按照指令，处理和总结简历文本。

【简历全文】:
{cv_text}

【目标公司】:
{target_company}

【你的任务】:
1.  **定位目标公司经历**: 从【简历全文】中，找出候选人在【目标公司】的工作经历。提取其职位和任职时间。将任职时间严格格式化为 "YY/M-YY/M" 或 "YY/M-Present"。
2.  **总结目标公司经历**: 用一句话（不超过50字）总结候选人在【目标公司】的核心职责或成就。请聚焦于个人，不要公司介绍。
3.  **罗列其他经历**: 找出【简历全文】中除目标公司外的所有其他工作经历。对于每一段经历，提取其公司名称、职位和任职时间（同样格式化为 "YY/M-YY/M" 或 "YY/M-Present"）。

【输出格式】:
请严格按照以下格式输出，不要添加任何多余的解释、标题或Markdown标记。

{target_company}的经历:
[在职时间] [公司名称] [职位]
[一句话总结]

其他工作经历:
[在职时间1] [公司名称1] [职位1]
[在职时间2] [公司名称2] [职位2]
"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": MODEL_ENDPOINT_ID,
        "max_completion_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()

        if 'error' in result:
            error_message = result['error']['message']
            print(f"--- AI Profile总结 API 返回错误: {error_message} ---")
            return f"AI_ERROR: {error_message}"

        summary = result.get('choices', [{}])[0].get('message', {}).get('content', None)

        if summary is None:
            print("--- AI Profile总结失败: API返回的content为None ---")
            return "AI_ERROR: API响应中无content字段。"
        
        if not summary.strip():
            print(f"--- AI Profile总结警告: API返回内容为空或仅有空白字符 ---")
            return f"AI_WARNING: 返回内容为空。Raw: '{summary}'"

        print("--- AI Profile总结成功 ---")
        return summary.strip()

    except requests.exceptions.RequestException as e:
        print(f"--- AI Profile总结 API 请求出错: {e} ---")
        return f"AI_ERROR: Request failed: {e}"
    except Exception as e:
        print(f"--- 处理 AI Profile总结响应时出错: {e} ---")
        return f"AI_ERROR: Exception during processing: {e}"


# --- [!!! 新增: 日期解析与比较辅助函数 !!!] ---

def convert_date_to_value(date_str):
    """
    将 'YY/M' 或 'Present' 格式的字符串转换为可比较的整数。
    例如: '24/4' -> 2404, 'Present' -> 999999
    """
    date_str = date_str.strip().upper()
    if date_str == "PRESENT":
        return 999999  # 代表“至今”的极大值
    
    # 匹配 YY/M 格式
    match = re.search(r"(\d{2})/(\d{1,2})", date_str)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        return year * 100 + month  # e.g., 24/4 变为 2404
    
    return 0  # 无法解析的日期默认为最早

def is_departure_date_ok(formatted_work_time, min_departure_str):
    """
    判断候选人的在职结束时间是否晚于(或等于)要求的“最早离职年限”。
    formatted_work_time: 'YY/M-YY/M' 或 'YY/M-Present'
    min_departure_str: 'YY/M' 或 'Present'
    """
    try:
        # 1. 获取候选人的在职结束时间字符串
        actual_end_date_str = formatted_work_time
        if '-' in formatted_work_time:
            parts = formatted_work_time.split('-')
            actual_end_date_str = parts[1].strip()  # 获取 '-' 后面的部分
        
        # 2. 将候选人结束时间和要求的最早离职时间转换为数值
        candidate_end_value = convert_date_to_value(actual_end_date_str)
        min_required_value = convert_date_to_value(min_departure_str)
        
        # 3. 比较：候选人的结束日期必须 >= 要求的最小日期
        return candidate_end_value >= min_required_value
    
    except Exception as e:
        print(f"--- 日期比较出错: {e} (在职时间: {formatted_work_time}, 要求: {min_departure_str}) ---")
        return False  # 安全起见，解析失败则判为不符合


def format_work_time(time_str):
    """
    将 (YYYY.MM - YYYY.MM, X年Y月) 或 (YYYY.MM - 至今, X年Y月)
    格式化为 YY/M-YY/M 或 YY/M-Present
    """
    try:
        # 1. 移除括号和逗号后的内容
        cleaned_str = time_str.strip("（）")
        if ',' in cleaned_str:
            cleaned_str = cleaned_str.split(',')[0].strip() # '2024.04 - 至今'

        # 2. 定义正则表达式
        # 匹配 YYYY.MM - YYYY.MM 或 YYYY.MM - 至今
        pattern = r"(\d{4})\.(\d{1,2})\s*-\s*(\d{4})\.(\d{1,2})|(\d{4})\.(\d{1,2})\s*-\s*(至今)"
        
        match = re.search(pattern, cleaned_str)
        
        if not match:
            # 可能是 '2024.04 - 至今' 这种
            if ' - 至今' in cleaned_str:
                parts = cleaned_str.split(' - 至今')
                start_match = re.search(r"(\d{4})\.(\d{1,2})", parts[0])
                if start_match:
                    start_year = start_match.group(1)[-2:] # '24'
                    start_month = int(start_match.group(2)) # 4
                    return f"{start_year}/{start_month}-Present"
            return cleaned_str # 无法解析，返回清理后的原样

        if match.group(1): # 匹配 YYYY.MM - YYYY.MM
            start_year = match.group(1)[-2:]
            start_month = int(match.group(2))
            end_year = match.group(3)[-2:]
            end_month = int(match.group(4))
            return f"{start_year}/{start_month}-{end_year}/{end_month}"
        
        elif match.group(5): # 匹配 YYYY.MM - 至今
            start_year = match.group(5)[-2:]
            start_month = int(match.group(6))
            return f"{start_year}/{start_month}-Present"
            
        return cleaned_str
    except Exception:
        return time_str # 出错时返回原始字符串

def format_name_to_initials(full_name, gender):
    """
    将姓名格式化为首字母缩写 + 先生/女士。
    例如: "周子南", "男" -> "Z先生"
    """
    if not full_name:
        return ""
    
    # 提取姓氏
    surname = full_name[0]
    
    try:
        # 获取姓氏的拼音首字母
        # pypinyin.pinyin会返回一个二维列表, 例如: [['zhōu']]
        # pypinyin.Style.FIRST_LETTER 会返回首字母, 例如: [['z']]
        pinyin_list = pypinyin.pinyin(surname, style=pypinyin.Style.FIRST_LETTER)
        first_char = pinyin_list[0][0].upper()
    except Exception:
        # 如果转换失败 (例如, 已经是英文字母), 则直接使用原字符
        first_char = surname.upper()

    # 根据性别添加称谓
    if gender == "男":
        return f"{first_char}先生"
    elif gender == "女":
        return f"{first_char}女士"
    else:
        # 如果没有性别信息，或者性别不是男/女，则只返回首字母
        return first_char

# --- [!!! 修改点 2: 新增线程安全的保存函数 !!!] ---
def save_data_to_excel():
    """
    线程安全地将全局 saved_contacts 保存到全局 output_filename。
    """
    # --- [!!! 修改: 引用全局计数器 !!!] ---
    global saved_contacts, output_filename, contacts_lock, qualified_resumes_count, processed_resumes_count
    
    print("\n--- 收到保存请求，正在保存当前数据... ---")
    
    with contacts_lock:
        if not output_filename:
            print("--- (保存失败) 输出文件名尚未设置 ---")
            return
        if not saved_contacts:
            print("--- (保存请求) 没有数据可保存 ---")
            # 即使没有数据，也要显示进度
            n = qualified_resumes_count
            m = processed_resumes_count
            print(f"--- (保存请求) 当前进度: {n}/{m} (合格/已看) ---")
            return
        
        # 创建数据的副本以尽快释放锁
        df = pd.DataFrame(list(saved_contacts)) # 使用 list() 创建副本
        
        if not df.empty:
            # 定义所需的列顺序
            desired_order = [
                '分类', '公司', '职位', '在职公司', '在职时间', 
                '云号码', '简历链接', 'Profile', '姓名', '是否合作', '最后一次登录时间'
            ]
            
            # 筛选出DataFrame中实际存在的列，并按desired_order排序
            cols_in_order = [col for col in desired_order if col in df.columns]
            df = df[cols_in_order]
            
            # 在第一列插入序号
            df.insert(0, '序号', range(1, 1 + len(df)))

        n = qualified_resumes_count # <-- 获取当前进度
        m = processed_resumes_count # <-- 获取当前进度

    # 在锁之外执行慢速的 I/O 操作
    try:
        df.to_excel(output_filename, index=False, engine='openpyxl')
        print(f"--- (保存请求) {len(df)} 条数据已成功保存到: {output_filename} ---")
        print(f"--- (保存请求) 当前进度: {n}/{m} (合格/已看) ---") # <-- 打印进度
    except Exception as e:
        print(f"--- (保存请求) 保存到 Excel 时出错: {e} ---")
# --- [!!! 修改结束 !!!] ---


# -------------------------------------------------------------------
# 3. 主自动化流程
# -------------------------------------------------------------------
async def main():
    
    # --- [!!! 修改点 3: 使用全局变量 !!!] ---
    global saved_contacts, output_filename, contacts_lock, qualified_resumes_count, processed_resumes_count
    
    # --- 1. 定义你的每日需求 (已修改为动态输入) ---
    print("\n--- 1. 定义你的每日需求 ---")
    
    # 清空上一轮的数据
    with contacts_lock:
        saved_contacts.clear()
        qualified_resumes_count = 0 # <-- 重置计数器 n
        processed_resumes_count = 0 # <-- 重置计数器 m
        
    # --- [!!! 新增: 获取分类和公司列表 !!!] ---
    category = input("请输入分类 (例如: 上游/下游): ").strip()
    companies_input = input("请输入一个或多个公司名，用'/'分隔 (例如: 公司A/公司B): ").strip()
    target_companies = [name.strip() for name in companies_input.split('/') if name.strip()]
    target_position = input("请输入目标职位 (例如: 产品经理): ").strip()
    # --- [!!! 修改结束 !!!] ---


    # --- [!!! 修改: 优化提纲建议的显示方式 !!!] ---
    # 1. 内部使用的模板，以便在循环中替换
    default_briefing_template = f"""
访谈提纲核心要求：
1. 必须有在 __COMPANY__ 的工作经历。
2. 职位与 {target_position} 相关。
"""

    # 2. 创建一个对用户友好的、合并了所有公司名的提纲，用于显示
    all_companies_str = " 或 ".join(target_companies)
    suggested_display_briefing = default_briefing_template.replace('__COMPANY__', all_companies_str)
    
    print("\n--- 建议的访谈提纲 ---")
    print(suggested_display_briefing)
    print("--- (程序将分别为每个公司进行精确搜索) ---")
    print("------------------------")
    
    use_default = input("是否使用上述建议提纲? (Y/n): ").strip().lower()
    
    if use_default == 'n':
        print("请输入你的自定义访谈提纲 (使用 '__COMPANY__' 作为公司占位符，程序会自动为每个公司替换并搜索):")
        lines = []
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
        briefing_template = "\n".join(lines)
    else:
        briefing_template = default_briefing_template
    # --- [!!! 修改结束 !!!] ---

    should_view_phone_input = input("是否需要查看联系方式? (默认为否) [y/N]: ").strip().lower()
    should_view_phone = should_view_phone_input == 'y'
    
    should_format_name_to_initials_input = input("姓名是否只保留首字母缩写 (例如: Z先生)? (默认为否) [y/N]: ").strip().lower()
    should_format_name_to_initials = should_format_name_to_initials_input == 'y'
        
    # --- [!!! 修改: 根据分类生成文件名 !!!] ---
    global output_filename # 确保修改的是全局变量
    default_filename = f"{category}_{target_position}_contacts.xlsx"
    user_filename = input(f"请输入输出文件名 (默认: {default_filename}): ").strip()
    if not user_filename:
        output_filename = default_filename
    else:
        output_filename = user_filename
        
    if not output_filename.endswith(".xlsx"):
        output_filename += ".xlsx"
    # --- [!!! 修改结束 !!!] ---
        
    # --- [!!! 新增: 获取最早离职年限 !!!] ---
    min_departure_str = input("请输入最早离职年限 (格式: YY/M, 例如 24/4。若要求在职请输入 'Present'): ").strip()
    if not min_departure_str:
        min_departure_str = "00/1"  # 设一个极早的默认值
        print("--- 未输入最早离职年限，默认不过滤 ---")

    # --- [!!! 新增: 获取最早的最后一次登录时间 !!!] ---
    earliest_login_date_str = input("请输入最早的最后一次登录时间 (格式: YYYY/MM/DD，可留空): ").strip()
    
    print("\n--- 配置确认 ---")
    print(f"分类: {category}")
    print(f"公司列表: {target_companies}")
    print(f"职位: {target_position}")
    print(f"文件: {output_filename}")
    print(f"最早离职: {min_departure_str}")
    print(f"最早登录: {earliest_login_date_str if earliest_login_date_str else '不过滤'}")
    # 使用对用户友好的、合并后的提纲进行确认
    final_briefing_display = briefing_template.replace('__COMPANY__', " 或 ".join(target_companies))
    print(f"最终提纲预览: \n{final_briefing_display}")
    print("--- (程序将分别为每个公司进行精确搜索) ---")
    print("------------------\n")
    # --- 动态输入结束 ---


    # --- 2. 初始化浏览器和数据存储 ---
    # saved_contacts = [] # <-- 已移至全局
    
    if not os.path.exists("state.json"):
        print("错误：未找到 state.json 登录文件。")
        print("请先运行 save_session() 函数并手动登录一次。")
        return

    async with async_playwright() as p:
        # headless=False 可以在调试时看到浏览器窗口
        browser = await p.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context(storage_state="state.json")
        page = await context.new_page()

        print("--- 自动化流程启动 ---")

        try:
            # --- [!!! 新增: 遍历公司列表的主循环 !!!] ---
            for target_company in target_companies:
                print(f"\n{'='*20} 开始处理公司: {target_company} {'='*20}\n")
                
                # 为当前公司生成具体的提纲
                briefing_text = briefing_template.replace('__COMPANY__', target_company)
                print(f"--- 当前公司提纲 ---\n{briefing_text}\n----------------------")

                # --- 3. 访问搜索页并搜索 ---
                await page.goto("https://h.liepin.com/search/getConditionItem") # 假设这是搜索页
                
                print(f"--- 正在为公司 '{target_company}' 执行搜索... ---")
                
                # <-- [Gemini 已保留你的修改] -->
                await page.fill('input#rc_select_1, input.search-input, input.company-position-input, .search-box, .search-input', f"{target_company} {target_position}")
                await page.click('button:has-text("搜 索"), button:has-text("搜索"), .search-btn, .submit-btn')

                print("搜索已提交，等待结果加载...")
                await page.wait_for_load_state('networkidle', timeout=10000)

                # Wait for page to load
                await page.wait_for_timeout(2000)  # 2-second wait for page to load
                
                page_number = 1
                while True:
                    print(f"\n--- 正在处理公司 '{target_company}' 的第 {page_number} 页 ---")

                    profile_link_selector = RESUME_LINK_SELECTOR
                    print(f"--- 使用预设选择器: '{profile_link_selector}' ---")
                    
                    # Give the page a moment to settle before looking for links
                    await page.wait_for_timeout(1000)
                    profile_links_locators = await page.locator(profile_link_selector).all()
                    
                    if not profile_links_locators:
                        if page_number == 1:
                            print(f"--- 公司 '{target_company}': 第 1 页未找到简历链接，将跳至下一个公司 ---")
                        else:
                            print(f"--- 公司 '{target_company}': 第 {page_number} 页未找到简历链接，已达末页，结束该公司处理 ---")
                        break # No links on the page, break pagination loop

                    total_links_on_page = len(profile_links_locators)
                    print(f"--- 公司 '{target_company}', 第 {page_number} 页: 共找到 {total_links_on_page} 个简历链接，开始筛选... ---")

                    for i, link_locator in enumerate(profile_links_locators): 
                        
                        # --- [!!! 修改点 5: 更新已处理计数器 m !!!] ---
                        with contacts_lock:
                            processed_resumes_count += 1
                        
                        print(f"\n--- 正在处理公司 '{target_company}' 的第 {page_number} 页, 第 {i+1}/{total_links_on_page} 个简历 (总计已看 {processed_resumes_count}) ---")
                        # --- [!!! 修改结束 !!!] ---
                        
                        # 检查是否需要暂停
                        while not pause_flag.is_set():
                            time.sleep(0.1)  # 暂停时短暂休眠
                        
                                            profile_page = None
                                            try:
                                                actual_login_date = "" # 初始化最后登录时间变量
                                                # <-- [Gemini 已确认] -->
                                                async with context.expect_page() as new_page_info:
                                                    await link_locator.click(timeout=5000) # 点击你找到的SOP'器
                                                
                                                profile_page = await new_page_info.value
                                                await profile_page.wait_for_load_state('domcontentloaded')
                                                profile_url = profile_page.url 
                                                # <-- [Gemini 逻辑结束] -->
                        
                        
                                                # Wait for page to load
                                                await profile_page.wait_for_timeout(2000)
                        
                                                # --- [!!! 新增: 提取并判断最后登录时间 !!!] ---
                                                try:
                                                    last_login_selector = "#resume-detail-single > div.Y9hQO > div > div.ant-tabs-nav > div.ant-tabs-extra-content > div > span:nth-child(3)"
                                                    last_login_text = await profile_page.locator(last_login_selector).text_content(timeout=5000)
                                                    
                                                    match = re.search(r'(\d{4}/\d{2}/\d{2})', last_login_text)
                                                    if not match:
                                                        raise ValueError("无法从文本中解析出YYYY/MM/DD格式的日期")
                                                    
                                                    actual_login_date = match.group(1) # 赋值给外部变量
                                                    print(f"--- 提取到最后登录时间: {actual_login_date} ---")
                        
                                                    # 如果用户输入了最早登录时间，则执行此项检查
                                                    if earliest_login_date_str:
                                                        if actual_login_date < earliest_login_date_str:
                                                            print(f"--- 登录时间不符: {actual_login_date} 早于要求的 {earliest_login_date_str}，跳过 ---")
                                                            continue
                                                        else:
                                                            print(f"--- 登录时间符合，继续处理 ---")
                                                except Exception as e:
                                                    # 如果提取失败，但用户又要求了最早登录时间，则必须跳过
                                                    if earliest_login_date_str:
                                                        print(f"--- 提取或判断 [最后登录时间] 失败: {e}，因已设置最早登录时间要求，跳过此人 ---")
                                                        continue
                                                    else:
                                                        print(f"--- 提取 [最后登录时间] 失败: {e}，但未设置要求，继续处理 ---")
                                                # --- [!!! 新增结束 !!!] ---
                        
                                                current_cv_selector = CV_TEXT_SELECTOR
                                                print(f"--- 使用预设CV文本选择器: '{current_cv_selector}' ---")
                                                
                                                cv_text = "" # 初始化
                                                try:
                                                    cv_text = await profile_page.locator(current_cv_selector).text_content(timeout=5000)
                                                except Exception as e:
                                                    print(f"提取简历文本失败: {e}。请检查选择器 {current_cv_selector}")
                                                    print("--- 调试SOP：请将这个新打开的“简历详情页”另存为 HTML，然后发给我。---")
                                                    continue
                                                
                                                work_time_selector = 'div.work-time, .work-duration, .time-text, .work-time-text, .contact-time, span.rd-work-time'
                                                raw_work_time = ""
                                                work_time = ""
                                                try:
                                                    raw_work_time = await profile_page.locator(work_time_selector).first.text_content(timeout=5000)
                                                    work_time = format_work_time(raw_work_time) # <-- 应用格式化
                                                    print(f"--- 提取在职时间: {work_time} (原始: {raw_work_time.strip()}) ---")
                                                except Exception as e:
                                                    print(f"--- 提取 [在职时间] 失败: {e}，跳过此人 ---")
                                                    continue
                        
                                                if not is_departure_date_ok(work_time, min_departure_str):
                                                    print(f"--- 日期不符: 候选人离职于 {work_time} (要求不早于 {min_departure_str})，跳过 ---")
                                                    continue
                                                else:
                                                    print(f"--- 日期符合: {work_time} (要求: {min_departure_str})，进入AI判断 ---")
                        
                        
                                                while not pause_flag.is_set():
                                                    time.sleep(0.1)
                                                
                                                if is_match_volc(cv_text, briefing_text):
                                                    print(f"AI 判断匹配: {profile_url}")
                        
                                                    # --- [!!! 新增逻辑: 提取在职公司并进行匹配 !!!] ---
                                                    company_selector = 'div.company-name, .work-company, .company-text, .company-title, .contact-company, div.rd-work-comp > h5'
                                                    company = ""
                                                    try:
                                                        company = await profile_page.locator(company_selector).first.text_content(timeout=5000)
                                                        print(f"--- 成功提取到 [在职公司] 用于匹配: {company.strip()} ---")
                                                    except Exception as e:
                                                        print(f"--- 提取 [在职公司] 失败: {e}，无法执行公司匹配，跳过 ---")
                                                        continue
                        
                                                    # 核心匹配逻辑: 用户搜索的公司名是否被包含于最近任职的公司名中 (忽略大小写)
                                                    if target_company.lower() not in company.lower():
                                                        print(f"--- 公司不匹配: 最近任职公司 '{company.strip()}' 中不包含搜索词 '{target_company}'，跳过 ---")
                                                        continue
                                                    else:
                                                        print(f"--- 公司匹配通过: '{company.strip()}' 包含 '{target_company}'，继续处理 ---")
                                                    # --- [!!! 新增逻辑结束 !!!] ---
                                                    
                                                    # --- [!!! 新增: 调用AI进行Profile总结 !!!] ---
                                                    print("--- 正在生成Profile总结... ---")
                                                    summarized_profile = summarize_profile_volc(cv_text, target_company)
                                                    # --- [!!! 新增结束 !!!] ---
                                                    
                                                    name = ""
                                                    gender = "" 
                                                    clean_name = "" 
                                                    # company 变量已在前面提取和使用
                                                    title = ""
                                                    contact_info = None
                        
                                                    name_selector = 'div.resume-preview-name, .person-name, .resume-name, .name-text, .contact-name, h4.name'
                                                    gender_info_selector = 'div.basic-cont > div.sep-info' # 包含性别、年龄、地区的行
                                                    title_selector = 'div.position-name, .work-position, .position-text, .position-title, .contact-position, h6.job-name'
                                                    
                                                    try:
                                                        name = await profile_page.locator(name_selector).first.text_content(timeout=5000) # 5秒超时
                                                    except Exception as e:
                                                        print(f"--- 提取 [姓名] 失败: {e} ---")
                                                        pass
                                                    
                                                    try:
                                                        info_text = await profile_page.locator(gender_info_selector).first.inner_text(timeout=5000)
                                                        gender_match = re.search(r'\s*(男|女)\s*', info_text)
                                                        if gender_match:
                                                            gender = gender_match.group(1)
                                                            print(f"--- G (G): {gender} ---")
                                                        else:
                                                            print(f"--- 未能从 '{info_text}' 中提取到性别 ---")
                                                    except Exception as e:
                                                        print(f"--- 提取 [性别] 失败: {e} ---")
                                                        pass
                        
                                                    clean_name = name.strip().replace("*", "") # <-- 移除星号
                                                    
                                                    if should_format_name_to_initials:
                                                        clean_name = format_name_to_initials(clean_name, gender)
                                                        print(f"--- 格式化为首字母缩写后 [姓名]: {clean_name} ---")
                                                    elif gender and "先生" not in clean_name and "女士" not in clean_name:
                                                        if gender == "男":
                                                            clean_name = clean_name + "先生"
                                                        elif gender == "女":
                                                            clean_name = clean_name + "女士"
                                                        print(f"--- 格式化后 [姓名]: {clean_name} ---")
                                                    else:
                                                        print(f"--- 成功提取到 [姓名]: {clean_name} (无需添加称谓) ---")
                                                    
                                                    # Note: The original 'company' extraction block is removed from here
                                                    # as it's now handled before the AI summary.
                                                        
                                                    try:
                                                        title = await profile_page.locator(title_selector).first.text_content(timeout=5000)
                                                        print(f"--- 成功提取到 [职位]: {title.strip()} ---")
                                                    except Exception as e:
                                                        print(f"--- 提取 [职位] 失败: {e} ---")
                                                        pass
                                                        
                                                    print(f"--- (确认) 在职时间: {work_time} ---")
                        
                                                    contact_info = None
                                                    if should_view_phone:
                                                        try:
                                                            cloud_phone_selector = '#resume-detail-basic-info > div.basic-cont > dl > dd:nth-child(1) > span.view-phone-btn, span.view-phone-btn:has-text("查看云电话")'
                                                            cloud_phone_button = profile_page.locator(cloud_phone_selector).first
                                                            
                                                            is_already_paid = False
                                                            try:
                                                                await cloud_phone_button.wait_for(state="visible", timeout=3000) 
                                                                print("--- (优先检查) 检测到“查看云电话”按钮，判定为已购买 ---")
                                                                await cloud_phone_button.click(timeout=3000) # 点击它以显示号码
                                                                await profile_page.wait_for_timeout(2000) # 等待号码加载
                                                                is_already_paid = True
                                                            except Exception:
                                                                print("--- (优先检查) 未检测到“查看云电话”按钮，判定为未购买 ---")
                                                                is_already_paid = False
                        
                                                            if not is_already_paid:
                                                                contact_button_selector = 'button:has-text("联系"), .get-chat-btn'
                                                                await profile_page.locator(contact_button_selector).first.click(timeout=5000)
                                                                print("--- 已点击“查看联系方式”按钮 ---")
                        
                                                                try:
                                                                    pay_button_selector = 'button:has-text("立即获得"), button:has-text("确认支付"), button:has-text("立即打开"), button:has-text("立即获取")'
                                                                    pay_button = profile_page.locator(pay_button_selector).first
                                                                    
                                                                    await pay_button.wait_for(state="visible", timeout=3000) # 等待最多3秒
                                                                    print("--- 检测到支付弹窗，尝试点击支付按钮 ---")
                                                                    await pay_button.click()
                                                                    await profile_page.wait_for_timeout(2000)
                        
                                                                except Exception as e:
                                                                    print(f"--- 未检测到支付弹窗 (或处理出错: {e})，直接进入下一步 ---")
                                                                    pass
                                                            
                                                            try:
                                                                image_selector = 'img[src*="liepin.com/v1/getcontact"]' # 使用更通用的图片src选择器
                                                                image_locator = profile_page.locator(image_selector).first
                                                                await image_locator.wait_for(state="visible", timeout=5000)
                                                                
                                                                print("--- 检测到图片格式的联系方式，准备截图 ---")
                                                                
                                                                name_for_file = clean_name if clean_name else f"Unknown_contact_{i+1}"
                                                                image_filename = f"{name_for_file}.png"
                                                                image_path = os.path.join(os.getcwd(), image_filename)
                                                                
                                                                await image_locator.screenshot(path=image_path)
                                                                
                                                                contact_info = image_path # 在Excel中记录图片的完整路径
                                                                print(f"--- 成功截图并保存为: {image_path} ---")
                        
                                                            except Exception:
                                                                print("--- 未找到图片格式的联系方式，尝试提取文本格式 ---")
                                                                try:
                                                                    await profile_page.wait_for_timeout(2000)  # 等待2秒让联系方式加载
                                                                    
                                                                    phone_selectors = [
                                                                        'div.cloud-phone h3', 
                                                                        '.contact-phone-text', 
                                                                        '#resume-detail-basic-info > div.basic-cont > dl > dd:nth-child(1) > span.view-phone-btn',
                                                                        'span.view-phone-btn',  # 简化选择器
                                                                        '.basic-cont dl dd span'  # 一般性选择器
                                                                    ]
                                                                    
                                                                    phone_number = None
                                                                    for selector in phone_selectors:
                                                                        try:
                                                                            phone_locator = profile_page.locator(selector).first
                                                                            await phone_locator.wait_for(state="visible", timeout=5000)
                                                                            phone_number = await phone_locator.text_content()
                                                                            if phone_number and phone_number.strip():  # 确保获取到非空文本
                                                                                break
                                                                        except Exception:
                                                                            continue  # 尝试下一个选择器
                                                                    
                                                                    if phone_number:
                                                                        cleaned_phone = phone_number.replace(" ", "") # 移除所有空格
                                                                        contact_info = f"云 {cleaned_phone}" # 云 后面加一个空格
                                                                        print(f"--- 成功提取文本联系方式: {contact_info} ---")
                                                                    else:
                                                                        print("--- 尝试从页面源码中查找电话号码 ---")
                                                                        page_content = await profile_page.content()
                                                                        phone_pattern = r'1[3-9]\d{9}'
                                                                        phone_matches = re.findall(phone_pattern, page_content)
                                                                        if phone_matches:
                                                                            contact_info = f"云 {phone_matches[0]}" # 匹配结果已经是无空格的
                                                                            print(f"--- 从页面源码中提取到电话号码: {contact_info} ---")
                                                                        else:
                                                                            raise ValueError("无法在页面上找到电话号码")
                                                                except Exception:
                                                                    print("--- 提取图片和文本联系方式均失败 ---")
                                                                    raise ValueError("无法找到联系方式")
                                                        except Exception as e:
                                                            print(f"提取联系方式的整体流程(步骤6)出错: {e}")
                                                    else:
                                                        print("--- 根据设置，跳过查看联系方式 ---")
                                                        contact_info = "未查看"
                        
                                                    # --- [!!! 修改: 保存数据时使用总结后的Profile !!!] ---
                                                    with contacts_lock:
                                                        saved_contacts.append({
                                                            "分类": category,
                                                            "公司": target_company,
                                                            "姓名": clean_name,
                                                            "职位": title.strip(),
                                                            "在职公司": company.strip(),
                                                            "在职时间": work_time.strip(),
                                                            "云号码": contact_info if contact_info else "获取失败",
                                                            "简历链接": profile_url,
                                                            "Profile": summarized_profile, # 使用总结内容
                                                            "是否合作": "否",
                                                            "最后一次登录时间": actual_login_date
                                                        })
                                                        qualified_resumes_count += 1
                                                    # --- [!!! 修改结束 !!!] ---
                                                    print(f"成功保存候选人: {clean_name}, 职位: {title.strip()}, 在职时间: {work_time.strip()}, 联系方式: {contact_info if contact_info else '获取失败'}")
                                                
                                                else:
                                                    print("AI 判断不匹配，跳过。")
                        
                                                
                                                while not pause_flag.is_set():
                                                    time.sleep(0.1)
                                                
                                                # --- [!!! 修改点 7: 打印当前进度 !!!] ---
                                                with contacts_lock:
                                                    n = qualified_resumes_count
                                                    m = processed_resumes_count
                                                print(f"--- 进度: {n}/{m} (合格/已看) ---")
                                                # --- [!!! 修改结束 !!!] ---
                        
                                            except Exception as e:
                                                print(f"处理第 {i+1} 个链接时发生未知错误: {e}")
                                                # --- [!!! 新增: 即使出错也打印进度 !!!] ---
                                                with contacts_lock:
                                                    n = qualified_resumes_count
                                                    m = processed_resumes_count
                                                print(f"--- (出错) 进度: {n}/{m} (合格/已看) ---")
                                                # --- [!!! 修改结束 !!!] ---
                                                # 错误会使我们落入 finally 块
                                            
                                            finally:
                                                if profile_page and not profile_page.is_closed():
                                                    await profile_page.close()
                                                
                                                # 无论成功、跳过还是出错，都在这里执行统一的随机暂停
                                                print("--- 执行随机延时 (3-7秒) ---")
                                                await page.wait_for_timeout(random.randint(3000, 7000))                    
                    # --- [!!! 新增: 翻页逻辑 !!!] ---
                    next_button_selector = "#resultList > div.table-box > table > tfoot > tr > td:nth-child(2) > ul > li.ant-pagination-next > button"
                    # 检查按钮是否被禁用的更可靠方法是检查其父 `li` 元素是否包含 `ant-pagination-disabled` 类
                    next_button_parent_disabled_selector = "li.ant-pagination-next.ant-pagination-disabled"

                    try:
                        # 检查 'next' 按钮的父级 li 是否被禁用
                        is_disabled = await page.locator(next_button_parent_disabled_selector).count() > 0
                        if is_disabled:
                            print(f"--- 已到达公司 '{target_company}' 的最后一页，翻页结束 ---")
                            break  # 退出 while 循环

                        next_button = page.locator(next_button_selector)
                        if await next_button.count() > 0:
                            print("--- 找到“下一页”按钮，准备翻页 ---")
                            await next_button.click(timeout=5000)
                            await page.wait_for_load_state('networkidle', timeout=10000)
                            await page.wait_for_timeout(2000)  # 等待内容渲染
                            page_number += 1
                        else:
                            print(f"--- 未找到“下一页”按钮，公司 '{target_company}' 的翻页结束 ---")
                            break  # 退出 while 循环
                    except Exception as e:
                        print(f"--- 翻页时发生错误: {e} ---")
                        print(f"--- 假定已是最后一页，公司 '{target_company}' 的翻页结束 ---")
                        break  # 出错时退出
                    # --- [!!! 新增结束 !!!] ---
            # --- [!!! 主循环结束 !!!] ---

        except Exception as e:
            print(f"主流程发生严重错误: {e}")
        
        finally:
            # --- [!!! 修改点 8: 调用新的保存函数 (已包含进度) !!!] ---
            print(f"\n--- 所有公司处理完毕！正在执行最终数据保存... ---")
            save_data_to_excel() # 使用新的保存函数
            # --- [!!! 修改结束 !!!] ---

            await browser.close()
            print("浏览器已关闭。")

def keyboard_listener():
    """监听键盘事件，用于暂停/继续功能"""
    try:
        from pynput import keyboard
        
        # --- [!!! 修改点 9: 修改 on_press 以便保存和打印进度 !!!] ---
        def on_press(key):
            # --- [!!! 新增: 引用全局计数器 !!!] ---
            global qualified_resumes_count, processed_resumes_count, contacts_lock
            
            try:
                if key == keyboard.Key.esc:
                    if pause_flag.is_set():
                        print("\n--- 程序暂停中，按 ESC 键继续 ---")
                        print("--- 正在保存当前进度... ---")
                        pause_flag.clear()  # 暂停
                        save_data_to_excel() # <-- 已包含进度打印
                        
                        # ( save_data_to_excel() 已经会打印进度了, 
                        #   为避免重复, 这里的额外打印可以注释掉,
                        #   但保留也无妨，作为明确的暂停反馈 )
                        with contacts_lock:
                            n = qualified_resumes_count
                            m = processed_resumes_count
                        print(f"--- (暂停时) 当前进度: {n}/{m} (合格/已看) ---")
                        
                    else:
                        print("\n--- 程序继续运行 ---")
                        pause_flag.set()  # 继续
            except AttributeError:
                pass  # 特殊按键不处理
        # --- [!!! 修改结束 !!!] ---

        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except ImportError:
        print("pynput库未安装，无法使用ESC暂停功能。请运行: pip install pynput")


def run_with_pause_control():
    """带有暂停控制和循环运行功能的主程序运行函数"""
    
    # 启动键盘监听线程
    listener_thread = threading.Thread(target=keyboard_listener, daemon=True)
    listener_thread.start()
    
    while True:
        # 运行主程序
        # 每次循环都会创建一个新的事件循环来运行 main()
        # 确保资源 (如浏览器) 被正确关闭和重新初始化
        try:
            pause_flag.set() # 确保每次循环开始时程序是运行状态
            asyncio.run(main())
        except Exception as e:
            print(f"--- 运行 main() 时发生意外错误: {e} ---")
            print("--- 准备进入下一轮... ---")

        print("\n" + "="*50)
        print("--- 本轮运行已结束 ---")
        print("="*50)
        
        choice = input("是否要用新的条件开始一轮新的搜索? [Y/n]: ").strip().lower()
        
        if choice == 'n':
            print("感谢使用，程序退出。")
            break
        # 如果输入 'y' 或直接按 Enter, 循环将继续
        # 并重新执行 main()，提示输入新的条件


# -------------------------------------------------------------------
# 4. 运行主程序
# -------------------------------------------------------------------
if __name__ == "__main__":
    
    # -----------------
    # !!! 重要 !!!
    # -----------------
    # 如果是第一次运行或登录过期，取消注释下一行来保存会话
    #asyncio.run(save_session())
    
    # 保存会话后，注释掉 save_session()，然后运行 main()
    run_with_pause_control()