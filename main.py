import asyncio
import os
import pandas as pd
import random
import json
import requests
from playwright.async_api import async_playwright
import threading
import time
import re
import pypinyin
import docx
from docx.shared import Pt
from bs4 import BeautifulSoup
import zipfile
import shutil
import sys
from datetime import datetime

# --- Rich Imports ---
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich import print as rprint

# Add local libs to path
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

sys.path.append(resource_path('libs'))

from htmldocx import HtmlToDocx

# Constants
VOLC_SECRETKEY = "0c0d7998-f994-49c5-afb6-072755da3c89"
RESUME_LINK_SELECTOR = "div.new-resume-personal-name"
CV_TEXT_SELECTOR = ".G0UQv"

# Global variables
pause_flag = threading.Event()
pause_flag.set()
contacts_lock = threading.Lock()
saved_contacts = []
output_filename = "" 
qualified_resumes_count = 0
processed_resumes_count = 0

console = Console()

# --- Input Manager for Backtracking ---
class InputManager:
    def __init__(self):
        self.steps = []
        self.data = {}

    def add_step(self, key, prompt, processor=None, default=None, required=False):
        self.steps.append({
            'key': key,
            'prompt': prompt,
            'processor': processor,
            'default': default,
            'required': required
        })

    def run(self):
        idx = 0
        while idx < len(self.steps):
            step = self.steps[idx]
            key = step['key']
            
            # Get current value (from previous run or default)
            current_val = self.data.get(key, step['default'])
            
            # Construct prompt
            prompt_text = f"[bold cyan]{step['prompt']}[/bold cyan]"
            if current_val is not None and current_val != "":
                prompt_text += f" [dim](默认: {current_val})[/dim]"
            
            console.print(f"\n[Step {idx+1}/{len(self.steps)}] {prompt_text}")
            console.print("[dim]输入 'b' 或 'back' 返回上一步[/dim]")
            
            user_input = input(">> ").strip()
            
            # Handle Backtracking
            if user_input.lower() in ['b', 'back']:
                if idx > 0:
                    idx -= 1
                    console.print("[yellow]Returning to previous step...[/yellow]")
                else:
                    console.print("[red]Already at the first step.[/red]")
                continue
            
            # Handle Default
            if not user_input:
                if step['required'] and (current_val is None or current_val == ""):
                    console.print("[red]此项为必填项，请输入。[/red]")
                    continue
                val = current_val
            else:
                val = user_input
            
            # Process & Validate
            if step['processor']:
                try:
                    processed_val = step['processor'](val)
                    self.data[key] = processed_val
                    idx += 1
                except Exception as e:
                    console.print(f"[red]输入无效: {e}[/red]")
            else:
                self.data[key] = val
                idx += 1
                
        return self.data

# --- Helper Functions ---

async def save_session():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto("https://h.liepin.com/search/getConditionItem")
        console.print(Panel("[bold yellow]请在弹出的浏览器窗口中手动登录猎聘网[/bold yellow]\n登录成功后，返回此终端，按 Enter 键继续", title="登录提示"))
        input()
        
        await context.storage_state(path="state.json")
        console.print("[green]登录状态已保存到 state.json。[/green]")
        await browser.close()

def is_match_volc(cv_text, briefing):
    api_key = VOLC_SECRETKEY
    if not api_key:
        console.print("[red]错误: 未找到 VOLC_SECRETKEY。[/red]")
        return False

    MODEL_ENDPOINT_ID = "doubao-seed-1-6-lite-251015"
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
    
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": MODEL_ENDPOINT_ID,
        "max_completion_tokens": 65535,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": "medium"
    }

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        if 'error' in result:
            console.print(f"[red]火山引擎 API 返回错误: {result['error']['message']}[/red]")
            return False

        answer = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip().upper()

        if not answer: return False
        
        color = "green" if "YES" in answer else "red"
        console.print(f"--- 火山引擎 AI 判断结果: [{color}]{answer}[/{color}] ---")
        return "YES" in answer

    except Exception as e:
        console.print(f"[red]火山引擎 API 请求出错: {e}[/red]")
        return False

def summarize_profile_volc(cv_text, target_company):
    api_key = VOLC_SECRETKEY
    if not api_key: return "错误: 未找到 VOLC_SECRETKEY。"

    MODEL_ENDPOINT_ID = "doubao-seed-1-6-lite-251015"
    API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

    prompt = f"""
    你是一位专业的简历分析师。
    【简历全文】: {cv_text}
    【目标公司】: {target_company}
    任务: 1.定位目标公司经历(YY/M-YY/M或Present) 2.一句话总结 3.罗列其他经历
    格式:
    {target_company}的经历:
    [在职时间] [公司名称] [职位]
    [一句话总结]
    其他工作经历:
    [在职时间1] [公司名称1] [职位1]
    """
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
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
        summary = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        if summary and summary.strip():
            console.print("[green]--- AI Profile总结成功 ---[/green]")
            return summary.strip()
        return "AI_WARNING: 返回内容为空。"
    except Exception as e:
        console.print(f"[red]AI Profile总结 API 请求出错: {e}[/red]")
        return f"AI_ERROR: {e}"

def convert_date_to_value(date_str):
    date_str = date_str.strip().upper()
    if date_str == "PRESENT": return 999999
    match = re.search(r"(\d{2})/(\d{1,2})", date_str)
    if match: return int(match.group(1)) * 100 + int(match.group(2))
    return 0

def parse_login_date_input(date_str):
    if not date_str: return None
    date_str = date_str.strip().replace('-', '/')
    formats = ["%Y/%m/%d", "%y/%m/%d", "%Y/%m", "%y/%m"]
    for fmt in formats:
        try: return datetime.strptime(date_str, fmt)
        except ValueError: continue
    console.print(f"[yellow]警告: 无法解析日期 '{date_str}'，将忽略此筛选条件。[/yellow]")
    return None

def is_departure_date_ok(formatted_work_time, min_departure_str):
    try:
        actual_end_date_str = formatted_work_time
        if '-' in formatted_work_time:
            parts = formatted_work_time.split('-')
            actual_end_date_str = parts[1].strip()
        return convert_date_to_value(actual_end_date_str) >= convert_date_to_value(min_departure_str)
    except Exception: return False

def format_work_time(time_str):
    try:
        cleaned_str = time_str.strip("（）")
        if ',' in cleaned_str: cleaned_str = cleaned_str.split(',')[0].strip()
        pattern = r"(\d{4})\.(\d{1,2})\s*-\s*(\d{4})\.(\d{1,2})|(\d{4})\.(\d{1,2})\s*-\s*(至今)"
        match = re.search(pattern, cleaned_str)
        if not match:
            if ' - 至今' in cleaned_str:
                parts = cleaned_str.split(' - 至今')
                start_match = re.search(r"(\d{4})\.(\d{1,2})", parts[0])
                if start_match: return f"{start_match.group(1)[-2:]}/{int(start_match.group(2))}-Present"
            return cleaned_str
        if match.group(1): return f"{match.group(1)[-2:]}/{int(match.group(2))}-{match.group(3)[-2:]}/{int(match.group(4))}"
        elif match.group(5): return f"{match.group(5)[-2:]}/{int(match.group(6))}-Present"
        return cleaned_str
    except Exception: return time_str

def format_name_to_initials(full_name, gender):
    if not full_name: return ""
    surname = full_name[0]
    try:
        pinyin_list = pypinyin.pinyin(surname, style=pypinyin.Style.FIRST_LETTER)
        first_char = pinyin_list[0][0].upper()
    except Exception: first_char = surname.upper()
    if gender == "男": return f"{first_char}先生"
    elif gender == "女": return f"{first_char}女士"
    return first_char

def save_resume_as_docx(html_content, filename):
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        for img in soup.find_all('img'):
            if img.get('src', '').startswith('data:'): img.decompose()
        for tag in soup.find_all(True):
            if 'style' in tag.attrs: del tag.attrs['style']
        
        doc = docx.Document()
        HtmlToDocx().add_html_to_document(str(soup), doc)
        doc.save(filename)
        console.print(f"[green]成功保存简历Docx: {filename}[/green]")
        return True
    except Exception as e:
        console.print(f"[red]保存Docx失败: {e}[/red]")
        return False

def zip_company_files(company_name, file_paths, output_zip_name):
    try:
        if not file_paths: return
        with zipfile.ZipFile(output_zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in file_paths:
                if os.path.exists(file_path): zipf.write(file_path, os.path.basename(file_path))
        console.print(f"[green]成功打包Zip: {output_zip_name} ({len(file_paths)} 个文件)[/green]")
    except Exception as e: console.print(f"[red]打包Zip失败: {e}[/red]")

def save_data_to_excel():
    global saved_contacts, output_filename, contacts_lock, qualified_resumes_count, processed_resumes_count
    console.print("\n[cyan]--- 收到保存请求，正在保存当前数据... ---[/cyan]")
    with contacts_lock:
        if not output_filename or not saved_contacts:
            console.print("[yellow]--- (保存请求) 没有数据或文件名未设置 ---[/yellow]")
            return
        
        df = pd.DataFrame(list(saved_contacts))
        if not df.empty:
            desired_order = ['分类', '公司', '职位', '在职公司', '在职时间', '云号码', '简历链接', 'Profile', '姓名', '是否合作', '最后一次登录时间']
            cols_in_order = [col for col in desired_order if col in df.columns]
            df = df[cols_in_order]
            df.insert(0, '序号', range(1, 1 + len(df)))
            if '公司' in df.columns: df.sort_values(by='公司', inplace=True)
        
        n, m = qualified_resumes_count, processed_resumes_count

    try:
        df.to_excel(output_filename, index=False, engine='openpyxl')
        console.print(f"[green]--- (保存请求) {len(df)} 条数据已成功保存到: {output_filename} ---[/green]")
        console.print(f"[bold]--- (保存请求) 当前进度: {n}/{m} (合格/已看) ---[/bold]")
    except Exception as e:
        console.print(f"[red]--- (保存请求) 保存到 Excel 时出错: {e} ---[/red]")

def clear_output_directories():
    dirs_to_clear = ['data', 'resumes', 'zips']
    console.print("\n[yellow]--- 正在清空输出目录... ---[/yellow]")
    for directory in dirs_to_clear:
        if os.path.exists(directory):
            try:
                for filename in os.listdir(directory):
                    file_path = os.path.join(directory, filename)
                    if os.path.isfile(file_path) or os.path.islink(file_path): os.unlink(file_path)
                    elif os.path.isdir(file_path): shutil.rmtree(file_path)
                console.print(f"[green]--- 已清空: {directory}/ ---[/green]")
            except Exception as e: console.print(f"[red]--- 清空 {directory}/ 失败: {e} ---[/red]")
        else: console.print(f"[dim]--- 目录不存在，跳过: {directory}/ ---[/dim]")
    console.print("[green]--- 清空完成 ---[/green]\n")

def archive_output_directories():
    """Moves existing files in data, resumes, zips to a timestamped archive folder."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"archive_{timestamp}"
    dirs_to_archive = ['data', 'resumes', 'zips']
    
    console.print(f"\n[yellow]--- 正在归档旧文件到 {archive_name}... ---[/yellow]")
    
    for directory in dirs_to_archive:
        if not os.path.exists(directory): continue
        
        # Create specific archive subfolder, e.g., data/archive_2023.../
        archive_path = os.path.join(directory, archive_name)
        if not os.path.exists(archive_path): os.makedirs(archive_path)
        
        try:
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                # Skip the archive folder itself
                if filename == archive_name: continue
                
                # Move file or directory
                shutil.move(file_path, os.path.join(archive_path, filename))
            console.print(f"[green]--- 已归档 {directory}/ 内容 ---[/green]")
        except Exception as e:
            console.print(f"[red]--- 归档 {directory}/ 失败: {e} ---[/red]")
    console.print("[green]--- 归档完成 ---[/green]\n")

def load_historical_data():
    """Loads all historical candidate data from xlsx files in 'data' directory (recursive)."""
    console.print("[dim]正在加载历史数据以进行查重...[/dim]")
    seen_candidates = set()
    data_dir = 'data'
    
    if not os.path.exists(data_dir): return seen_candidates
    
    xlsx_files = []
    for root, dirs, files in os.walk(data_dir):
        for file in files:
            if file.endswith(".xlsx") and not file.startswith("~$"): # Ignore temp files
                xlsx_files.append(os.path.join(root, file))
    
    for file_path in xlsx_files:
        try:
            df = pd.read_excel(file_path)
            # Ensure required columns exist
            required_cols = ['姓名', '职位', '在职时间']
            if all(col in df.columns for col in required_cols):
                for _, row in df.iterrows():
                    # Create a tuple signature for the candidate
                    # Using str() to ensure consistent types
                    signature = (
                        str(row['姓名']).strip(),
                        str(row['职位']).strip(),
                        str(row['在职时间']).strip()
                    )
                    seen_candidates.add(signature)
        except Exception as e:
            console.print(f"[yellow]读取历史文件失败 {os.path.basename(file_path)}: {e}[/yellow]")
            
    console.print(f"[green]已加载 {len(seen_candidates)} 条历史记录用于查重。[/green]")
    return seen_candidates

def ensure_browsers_installed():
    """Check if Playwright browsers are installed, if not, install them."""
    console.print("[dim]正在检查浏览器环境...[/dim]")
    try:
        # Try to launch a browser to see if it works
        import subprocess
        # This is a simple check. A more robust way is to just run 'playwright install chromium'
        # but that takes time. Let's try to run the install command only if needed.
        # Actually, for a CLI tool, it's safer to just run 'playwright install chromium' 
        # but we can suppress output if it's already installed.
        # However, 'playwright install' checks itself.
        
        console.print("[dim]正在验证/安装 Chromium 浏览器... (首次运行可能需要几分钟)[/dim]")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        console.print("[green]浏览器环境检查通过。[/green]")
    except Exception as e:
        console.print(f"[yellow]警告: 浏览器安装检查失败 ({e})。如果程序运行报错，请手动运行 'playwright install'。[/yellow]")

# --- Main Logic ---

async def main():
    global saved_contacts, output_filename, contacts_lock, qualified_resumes_count, processed_resumes_count
    
    # --- Setup ---
    console.rule("[bold blue]猎聘简历自动化助手[/bold blue]")
    
    ensure_browsers_installed()
    
    if Confirm.ask("是否需要重新登录/更新Cookie?"):
        await save_session()
    
    if Confirm.ask("是否清空 data, resumes, zips 文件夹下的所有内容? (y=清空, n=归档)"):
        clear_output_directories()
    else:
        archive_output_directories()
        
    # Load historical data for deduplication
    seen_candidates = load_historical_data()
    
    # --- Input Phase with Backtracking ---
    im = InputManager()
    
    im.add_step('category', "请输入分类 (例如: 上游/下游)", required=True)
    im.add_step('companies', "请输入公司和配额，用'/'分隔 (格式: 公司A 10/公司B 5)", required=True)
    im.add_step('positions', "请输入目标职位 (例如: 产品经理/数据分析师)", default="产品经理")
    
    # Briefing Template Logic
    def process_briefing(val):
        if val.lower() == 'y': return "DEFAULT"
        return "CUSTOM"
    
    im.add_step('use_default_briefing', "是否使用建议提纲? (Y/n)", default='y', processor=process_briefing)
    
    # Conditional step for custom briefing (handled in loop or just ask if needed)
    # Since InputManager is linear, we can just ask for custom briefing if needed, or handle it after.
    # Let's handle it after for simplicity, or add it as an optional step.
    
    im.add_step('view_phone', "是否需要查看联系方式? (y/N)", default='n')
    im.add_step('format_name', "姓名是否只保留首字母缩写? (y/N)", default='n')
    im.add_step('filename', "请输入输出文件名", default="output.xlsx")
    im.add_step('min_departure', "离职年限不早于 (格式: YY/M 或 'Present')", default="Present")
    im.add_step('earliest_login', "最后一次登陆时间不晚于 (格式: YY/M)", default="")
    im.add_step('zip_id', "请输入压缩包命名标识", default="ZTZ")
    
    data = im.run()
    
    # Process Inputs
    category = data['category']
    companies_input = data['companies']
    target_companies_info = []
    if companies_input:
        for entry in companies_input.split('/'):
            if not entry.strip(): continue
            parts = entry.strip().rsplit(' ', 1)
            if len(parts) == 2 and parts[1].isdigit():
                target_companies_info.append({'name': parts[0].strip(), 'quota': int(parts[1])})
            else:
                target_companies_info.append({'name': entry.strip(), 'quota': float('inf')})
    
    target_positions = [p.strip() for p in data['positions'].split('/') if p.strip()]
    
    # Briefing
    target_position_str = "/".join(target_positions)
    all_companies_str = " 或 ".join([info['name'] for info in target_companies_info])
    default_briefing = f"""
访谈提纲核心要求：
1. 必须有在 {all_companies_str} 的工作经历。
2. 职位与 {target_position_str} 相关。
"""
    if data['use_default_briefing'] == "DEFAULT":
        briefing_template = default_briefing
        console.print(Panel(briefing_template, title="使用建议提纲"))
    else:
        console.print("[yellow]请输入你的自定义访谈提纲 (输入END结束):[/yellow]")
        lines = []
        while True:
            line = input()
            if line.strip().upper() == "END": break
            lines.append(line)
        briefing_template = "\n".join(lines)

    should_view_phone = data['view_phone'].lower() == 'y'
    should_format_name = data['format_name'].lower() == 'y'
    
    user_filename = data['filename']
    if not user_filename.endswith(".xlsx"): user_filename += ".xlsx"
    output_filename = os.path.join('data', user_filename)
    
    min_departure_str = data['min_departure']
    earliest_login_date = parse_login_date_input(data['earliest_login'])
    zip_identifier = data['zip_id']
    
    # --- Confirmation Table ---
    table = Table(title="配置确认")
    table.add_column("配置项", style="cyan")
    table.add_column("值", style="magenta")
    table.add_row("分类", category)
    table.add_row("职位", target_position_str)
    table.add_row("输出文件", output_filename)
    table.add_row("最早离职", min_departure_str)
    table.add_row("最早登录", earliest_login_date.strftime('%Y/%m/%d') if earliest_login_date else "不过滤")
    console.print(table)
    
    # --- Execution ---
    for folder in ['resumes', 'data', 'zips']:
        if not os.path.exists(folder): os.makedirs(folder)
        
    if not os.path.exists("state.json"):
        console.print("[red]错误：未找到 state.json。请先登录。[/red]")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel='chrome', args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(storage_state="state.json")
        page = await context.new_page()
        
        console.print("[bold green]--- 自动化流程启动 ---[/bold green]")
        
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TextColumn("[bold cyan]({task.fields[qualified]}/{task.fields[processed]})[/bold cyan]"), # Custom column for stats
                TimeElapsedColumn(),
                console=console
            ) as progress:
                
                for company_info in target_companies_info:
                    target_company = company_info['name']
                    company_quota = company_info['quota']
                    current_company_qualified_count = 0
                    company_generated_files = []
                    
                    # Initialize task with custom fields
                    task_id = progress.add_task(
                        f"[cyan]处理公司: {target_company}", 
                        total=company_quota if company_quota != float('inf') else 100,
                        qualified=0,
                        processed=0
                    )
                    
                    briefing_text = briefing_template.replace('__COMPANY__', target_company)
                    
                    for current_position in target_positions:
                        if current_company_qualified_count >= company_quota: break
                        
                        console.print(f"\n[dim]正在搜索职位: {current_position}[/dim]")
                        await page.goto("https://h.liepin.com/search/getConditionItem")
                        
                        consecutive_failure_count = 0
                        await page.fill('input#rc_select_1, input.search-input, input.company-position-input, .search-box, .search-input', f"{target_company} {current_position}")
                        await page.click('button:has-text("搜 索"), button:has-text("搜索"), .search-btn, .submit-btn')
                        
                        await page.wait_for_load_state('networkidle', timeout=10000)
                        await page.wait_for_timeout(3000)
                        
                        page_number = 1
                        while True:
                            if consecutive_failure_count >= 10: break
                            
                            # console.print(f"[dim]第 {page_number} 页[/dim]")
                            await page.wait_for_timeout(1000)
                            profile_links_locators = await page.locator(RESUME_LINK_SELECTOR).all()
                            
                            if not profile_links_locators: break
                            
                            for i, link_locator in enumerate(profile_links_locators):
                                if consecutive_failure_count >= 10: break
                                
                                with contacts_lock:
                                    processed_resumes_count += 1
                                
                                while not pause_flag.is_set(): time.sleep(0.1)
                                
                                profile_page = None
                                try:
                                    async with context.expect_page() as new_page_info:
                                        await link_locator.click(timeout=5000)
                                    profile_page = await new_page_info.value
                                    await profile_page.wait_for_load_state('domcontentloaded')
                                    await profile_page.wait_for_timeout(2000)
                                    
                                    # Logic Checks (Login, Date, AI, Company)
                                    # ... (Simplified for brevity, logic remains same as before)
                                    
                                    # 1. Login Date
                                    try:
                                        last_login_text = await profile_page.locator("#resume-detail-single > div.Y9hQO > div > div.ant-tabs-nav > div.ant-tabs-extra-content > div > span:nth-child(3)").text_content(timeout=5000)
                                        match = re.search(r'(\d{4}/\d{2}/\d{2})', last_login_text)
                                        if not match: raise ValueError("无法解析日期")
                                        actual_login_date_str = match.group(1)
                                        actual_login_date_dt = datetime.strptime(actual_login_date_str, "%Y/%m/%d")
                                        
                                        if earliest_login_date and actual_login_date_dt < earliest_login_date:
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=processed_resumes_count)
                                            continue
                                    except Exception:
                                        if earliest_login_date:
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=processed_resumes_count)
                                            continue

                                    # 2. Work Time
                                    try:
                                        work_time_selector = 'div.work-time, .work-duration, .time-text, .work-time-text, .contact-time, span.rd-work-time'
                                        raw_work_time = await profile_page.locator(work_time_selector).first.text_content(timeout=5000)
                                        work_time = format_work_time(raw_work_time)
                                        if not is_departure_date_ok(work_time, min_departure_str):
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=processed_resumes_count)
                                            continue
                                    except Exception: continue

                                    # 3. AI
                                    cv_text = await profile_page.locator(CV_TEXT_SELECTOR).text_content(timeout=5000)
                                    if not is_match_volc(cv_text, briefing_text):
                                        consecutive_failure_count += 1
                                        progress.update(task_id, processed=processed_resumes_count)
                                        continue
                                    
                                    # 4. Company
                                    company_selector = 'div.company-name, .work-company, .company-text, .company-title, .contact-company, div.rd-work-comp > h5'
                                    company = await profile_page.locator(company_selector).first.text_content(timeout=5000)
                                    if target_company.lower() not in company.lower():
                                        consecutive_failure_count += 1
                                        progress.update(task_id, processed=processed_resumes_count)
                                        continue
                                    
                                    # Success
                                    summarized_profile = summarize_profile_volc(cv_text, target_company)
                                    name = await profile_page.locator('div.resume-preview-name, .person-name, .resume-name, .name-text, .contact-name, h4.name').first.text_content(timeout=5000)
                                    clean_name = name.strip().replace("*", "")
                                    
                                    gender = ""
                                    try:
                                        info_text = await profile_page.locator('div.basic-cont > div.sep-info').first.inner_text(timeout=5000)
                                        gender = re.search(r'\s*(男|女)\s*', info_text).group(1)
                                    except: pass
                                    
                                    if should_format_name:
                                        clean_name = format_name_to_initials(clean_name, gender)
                                    elif gender and "先生" not in clean_name and "女士" not in clean_name:
                                        clean_name += f"{gender}士" if gender == "女" else "先生"
                                    
                                    title = await profile_page.locator('div.position-name, .work-position, .position-text, .position-title, .contact-position, h6.job-name').first.text_content(timeout=5000)
                                    
                                    # --- Deduplication Check ---
                                    candidate_signature = (clean_name.strip(), title.strip(), work_time.strip())
                                    if candidate_signature in seen_candidates:
                                        console.print(f"[yellow]发现重复候选人: {clean_name} - {title}，跳过。[/yellow]")
                                        consecutive_failure_count += 1 # Treat duplicate as failure to trigger early stopping if too many
                                        progress.update(task_id, processed=processed_resumes_count)
                                        continue
                                    
                                    # Add to seen set to prevent duplicates within the same run
                                    seen_candidates.add(candidate_signature)
                                    # ---------------------------
                                    
                                    contact_info = "未查看"
                                    if should_view_phone:
                                        contact_info = "需手动查看" # Simplified
                                    
                                    with contacts_lock:
                                        saved_contacts.append({
                                            "分类": category,
                                            "公司": target_company,
                                            "姓名": clean_name,
                                            "职位": title.strip(),
                                            "在职公司": company.strip(),
                                            "在职时间": work_time.strip(),
                                            "云号码": contact_info,
                                            "简历链接": profile_page.url,
                                            "Profile": summarized_profile, 
                                            "是否合作": "否",
                                            "最后一次登录时间": actual_login_date_str
                                        })
                                        qualified_resumes_count += 1
                                        current_company_qualified_count += 1
                                    
                                    full_html = await profile_page.content()
                                    base_filename = f"{qualified_resumes_count}-猎聘-{clean_name}"
                                    docx_filename = os.path.join('resumes', f"{base_filename}.docx")
                                    counter = 1
                                    while os.path.exists(docx_filename):
                                        docx_filename = os.path.join('resumes', f"{base_filename}-{counter}.docx")
                                        counter += 1
                                    
                                    if save_resume_as_docx(full_html, docx_filename):
                                        company_generated_files.append(docx_filename)
                                    
                                    consecutive_failure_count = 0
                                    progress.update(task_id, advance=1, qualified=qualified_resumes_count, processed=processed_resumes_count)
                                    
                                    if current_company_qualified_count >= company_quota:
                                        break

                                except Exception as e:
                                    console.print(f"[red]处理出错: {e}[/red]")
                                finally:
                                    if profile_page: await profile_page.close()
                                    await page.wait_for_timeout(random.randint(3000, 7000))
                            
                            if current_company_qualified_count >= company_quota: break
                            
                            next_btn = page.locator("#resultList > div.table-box > table > tfoot > tr > td:nth-child(2) > ul > li.ant-pagination-next > button")
                            if await next_btn.count() > 0 and not await page.locator("li.ant-pagination-next.ant-pagination-disabled").count() > 0:
                                await next_btn.click()
                                await page.wait_for_load_state('networkidle')
                                page_number += 1
                            else: break
                    
                    if company_generated_files:
                        zip_name = os.path.join('zips', f"猎聘-{target_company}-{len(company_generated_files)}份-{zip_identifier}.zip")
                        counter = 1
                        base = zip_name.replace(".zip", "")
                        while os.path.exists(zip_name):
                            zip_name = f"{base}-{counter}.zip"
                            counter += 1
                        zip_company_files(target_company, company_generated_files, zip_name)

        finally:
            save_data_to_excel()
            await browser.close()

def keyboard_listener():
    try:
        from pynput import keyboard
        def on_press(key):
            global pause_flag
            try:
                if key == keyboard.Key.esc:
                    if pause_flag.is_set():
                        console.print("\n[yellow]--- 暂停中... ---[/yellow]")
                        pause_flag.clear()
                        save_data_to_excel()
                    else:
                        console.print("\n[green]--- 继续运行 ---[/green]")
                        pause_flag.set()
            except AttributeError: pass
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except ImportError:
        console.print("pynput未安装")

def run():
    listener_thread = threading.Thread(target=keyboard_listener, daemon=True)
    listener_thread.start()
    
    while True:
        try:
            pause_flag.set()
            asyncio.run(main())
        except Exception as e:
            console.print(f"[red]运行出错: {e}[/red]")
        
        if not Confirm.ask("是否开始新一轮搜索?"):
            break

if __name__ == "__main__":
    run()
