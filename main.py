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

# --- Helper Functions ---

def resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

sys.path.append(resource_path('libs'))

from datetime import datetime
from typing import List, Dict, Set, Optional, Tuple
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Rich Imports ---
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, TaskID
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich import print as rprint

from htmldocx import HtmlToDocx

# --- Configuration & Constants ---
VOLC_SECRETKEY = os.getenv("VOLC_SECRETKEY")
RESUME_LINK_SELECTOR = "div.new-resume-personal-name"
CV_TEXT_SELECTOR = "#resume-detail-single"

console = Console()

def convert_date_to_value(date_str: str) -> int:
    date_str = date_str.strip().upper()
    if date_str == "PRESENT": return 999999
    match = re.search(r"(\d{2})/(\d{1,2})", date_str)
    if match: return int(match.group(1)) * 100 + int(match.group(2))
    return 0

def parse_login_date_input(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    date_str = date_str.strip().replace('-', '/')
    formats = ["%Y/%m/%d", "%y/%m/%d", "%Y/%m", "%y/%m"]
    for fmt in formats:
        try: return datetime.strptime(date_str, fmt)
        except ValueError: continue
    console.print(f"[yellow]警告: 无法解析日期 '{date_str}'，将忽略此筛选条件。[/yellow]")
    return None

def is_departure_date_ok(formatted_work_time: str, min_departure_str: str) -> bool:
    try:
        actual_end_date_str = formatted_work_time
        if '-' in formatted_work_time:
            parts = formatted_work_time.split('-')
            actual_end_date_str = parts[1].strip()
        return convert_date_to_value(actual_end_date_str) >= convert_date_to_value(min_departure_str)
    except Exception: return False

def format_work_time(time_str: str) -> str:
    try:
        cleaned_str = time_str.strip("（）")
        if ',' in cleaned_str: cleaned_str = cleaned_str.split(',')[0].strip()
        
        # 支持中英文的 "至今/To present/present" 格式
        pattern = r"(\d{4})\.(\d{1,2})\s*-\s*(\d{4})\.(\d{1,2})|(\d{4})\.(\d{1,2})\s*-\s*(至今|To present|present)"
        match = re.search(pattern, cleaned_str, re.IGNORECASE)
        
        if not match:
            # 备用匹配：处理 " - 至今" 或 " - To present" 格式
            if re.search(r'\s*-\s*(至今|To present|present)', cleaned_str, re.IGNORECASE):
                parts = re.split(r'\s*-\s*(?:至今|To present|present)', cleaned_str, flags=re.IGNORECASE)
                start_match = re.search(r"(\d{4})\.(\d{1,2})", parts[0])
                if start_match: return f"{start_match.group(1)[-2:]}/{int(start_match.group(2))}-Present"
            return cleaned_str
        
        if match.group(1): 
            return f"{match.group(1)[-2:]}/{int(match.group(2))}-{match.group(3)[-2:]}/{int(match.group(4))}"
        elif match.group(5): 
            return f"{match.group(5)[-2:]}/{int(match.group(6))}-Present"
        return cleaned_str
    except Exception: return time_str

def extract_name_first_char(name: str) -> str:
    """提取姓名的第一个字符用于查重匹配"""
    if not name:
        return ""
    # 移除常见后缀后提取第一个字符
    clean = name.strip().replace("*", "").replace("先生", "").replace("女士", "")
    return clean[0] if clean else ""

def format_name_to_initials(full_name: str, gender: str) -> str:
    if not full_name: return ""
    surname = full_name[0]
    try:
        pinyin_list = pypinyin.pinyin(surname, style=pypinyin.Style.FIRST_LETTER)
        first_char = pinyin_list[0][0].upper()
    except Exception: first_char = surname.upper()
    if gender == "男": return f"{first_char}先生"
    elif gender == "女": return f"{first_char}女士"
    return first_char

def save_resume_as_docx(html_content: str, filename: str, max_retries: int = 3) -> bool:
    """保存简历为 docx 文件，支持失败重试机制"""
    for attempt in range(max_retries):
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
            if attempt < max_retries - 1:
                console.print(f"[yellow]保存Docx失败 (尝试 {attempt+1}/{max_retries}): {e}，重试中...[/yellow]")
                time.sleep(1)  # 短暂等待后重试
            else:
                console.print(f"[red]保存Docx最终失败 (已重试 {max_retries} 次): {e}[/red]")
                return False
    return False

def zip_company_files(company_name: str, file_paths: List[str], output_zip_name: str):
    try:
        if not file_paths: return
        with zipfile.ZipFile(output_zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in file_paths:
                if os.path.exists(file_path): zipf.write(file_path, os.path.basename(file_path))
        console.print(f"[green]成功打包Zip: {output_zip_name} ({len(file_paths)} 个文件)[/green]")
    except Exception as e: console.print(f"[red]打包Zip失败: {e}[/red]")

# --- AI Functions ---
def is_match_volc(cv_text: str, briefing: str, max_retries: int = 3) -> Optional[bool]:
    """判断简历是否匹配，返回 True/False/None (None表示API错误)"""
    api_key = VOLC_SECRETKEY
    if not api_key:
        console.print("[red]错误: 未找到 VOLC_SECRETKEY。[/red]")
        return None

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

    for attempt in range(max_retries):
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            
            if 'error' in result:
                error_msg = result['error']['message']
                if attempt < max_retries - 1:
                    console.print(f"[yellow]火山引擎 API 错误 (尝试 {attempt+1}/{max_retries}): {error_msg}，重试中...[/yellow]")
                    import time
                    time.sleep(2 ** attempt)  # 指数退避: 1s, 2s, 4s
                    continue
                else:
                    console.print(f"[red]火山引擎 API 返回错误: {error_msg}[/red]")
                    return None

            answer = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip().upper()
            if not answer:
                if attempt < max_retries - 1:
                    console.print(f"[yellow]AI 返回空结果 (尝试 {attempt+1}/{max_retries})，重试中...[/yellow]")
                    import time
                    time.sleep(2 ** attempt)
                    continue
                return None
            
            color = "green" if "YES" in answer else "red"
            console.print(f"--- 火山引擎 AI 判断结果: [{color}]{answer}[/{color}] ---")
            return "YES" in answer

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                console.print(f"[yellow]API 请求超时 (尝试 {attempt+1}/{max_retries})，重试中...[/yellow]")
                import time
                time.sleep(2 ** attempt)
                continue
            else:
                console.print(f"[red]火山引擎 API 请求超时 (已重试 {max_retries} 次)[/red]")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                console.print(f"[yellow]API 请求出错 (尝试 {attempt+1}/{max_retries}): {e}，重试中...[/yellow]")
                import time
                time.sleep(2 ** attempt)
                continue
            else:
                console.print(f"[red]火山引擎 API 请求出错: {e}[/red]")
                return None
    
    return None

def summarize_profile_volc(cv_text: str, target_company: str) -> str:
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

# --- Input Manager ---
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
            current_val = self.data.get(key, step['default'])
            
            prompt_text = f"[bold cyan]{step['prompt']}[/bold cyan]"
            if current_val is not None and current_val != "":
                prompt_text += f" [dim](默认: {current_val})[/dim]"
            
            console.print(f"\n[Step {idx+1}/{len(self.steps)}] {prompt_text}")
            console.print("[dim]输入 'b' 或 'back' 返回上一步[/dim]")
            
            user_input = input(">> ").strip()
            
            if user_input.lower() in ['b', 'back']:
                if idx > 0:
                    idx -= 1
                    console.print("[yellow]Returning to previous step...[/yellow]")
                else:
                    console.print("[red]Already at the first step.[/red]")
                continue
            
            if not user_input:
                if step['required'] and (current_val is None or current_val == ""):
                    console.print("[red]此项为必填项，请输入。[/red]")
                    continue
                val = current_val
            else:
                val = user_input
            
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

# --- Main Scraper Class ---

class LiepinScraper:
    def __init__(self):
        self.pause_flag = threading.Event()
        self.pause_flag.set()
        self.contacts_lock = threading.Lock()
        self.saved_contacts = []
        self.output_filename = ""
        self.qualified_resumes_count = 0
        self.processed_resumes_count = 0
        self.seen_candidates: Set[Tuple[str, str, str]] = set()
        
        # Configuration
        self.config = {}
        self.target_companies_info = []
        self.target_positions = []
        self.briefing_template = ""
        self.is_default_filename = False
        self.actually_searched_positions = []
        self.base_default_filename = "" # 分类-公司名 部分
        
    def ensure_browsers_installed(self):
        console.print("[dim]正在检查浏览器环境...[/dim]")
        try:
            import subprocess
            console.print("[dim]正在验证/安装 Chromium 浏览器... (首次运行可能需要几分钟)[/dim]")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            console.print("[green]浏览器环境检查通过。[/green]")
        except Exception as e:
            console.print(f"[yellow]警告: 浏览器安装检查失败 ({e})。如果程序运行报错，请手动运行 'playwright install'。[/yellow]")

    async def save_session(self):
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

    def clear_output_directories(self):
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

    def archive_output_directories(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"archive_{timestamp}"
        dirs_to_archive = ['data', 'resumes', 'zips']
        
        console.print(f"\n[yellow]--- 正在归档旧文件到 {archive_name}... ---[/yellow]")
        
        for directory in dirs_to_archive:
            if not os.path.exists(directory): continue
            archive_path = os.path.join(directory, archive_name)
            if not os.path.exists(archive_path): os.makedirs(archive_path)
            
            try:
                for filename in os.listdir(directory):
                    file_path = os.path.join(directory, filename)
                    if filename == archive_name: continue
                    shutil.move(file_path, os.path.join(archive_path, filename))
                console.print(f"[green]--- 已归档 {directory}/ 内容 ---[/green]")
            except Exception as e:
                console.print(f"[red]--- 归档 {directory}/ 失败: {e} ---[/red]")
        console.print("[green]--- 归档完成 ---[/green]\n")

    def load_historical_data(self):
        console.print("[dim]正在加载历史数据以进行查重...[/dim]")
        self.seen_candidates = set()
        data_dir = 'data'
        
        if not os.path.exists(data_dir): return
        
        xlsx_files = []
        for root, dirs, files in os.walk(data_dir):
            for file in files:
                if file.endswith(".xlsx") and not file.startswith("~$"):
                    xlsx_files.append(os.path.join(root, file))
        
        for file_path in xlsx_files:
            try:
                df = pd.read_excel(file_path)
                required_cols = ['姓名', '职位', '在职时间']
                if all(col in df.columns for col in required_cols):
                    for _, row in df.iterrows():
                        signature = (
                            extract_name_first_char(str(row['姓名'])),
                            str(row['职位']).strip(),
                            str(row['在职时间']).strip()
                        )
                        self.seen_candidates.add(signature)
            except Exception as e:
                console.print(f"[yellow]读取历史文件失败 {os.path.basename(file_path)}: {e}[/yellow]")
                
        console.print(f"[green]已加载 {len(self.seen_candidates)} 条历史记录用于查重。[/green]")

    def get_user_inputs(self):
        # 1. 基础信息预收集 (为了生成默认文件名)
        category = Prompt.ask("[bold cyan]请输入分类 (例如: 上游/下游)[/bold cyan]")
        companies_str = Prompt.ask("[bold cyan]请输入公司和配额，用'/'分隔 (格式: 公司A 10/公司B 5)[/bold cyan]")
        positions_str = Prompt.ask("[bold cyan]请输入关键词 (例如: 产品经理-数据分析师)[/bold cyan]", default="", show_default=False)

        # 解析公司和职位
        target_companies_info = []
        for entry in companies_str.split('/'):
            if not entry.strip(): continue
            parts = entry.strip().rsplit(' ', 1)
            if len(parts) == 2 and parts[1].isdigit():
                target_companies_info.append({'name': parts[0].strip(), 'quota': int(parts[1])})
            else:
                target_companies_info.append({'name': entry.strip(), 'quota': float('inf')})
        
        target_positions = [p.strip() for p in positions_str.split('-') if p.strip()]
        
        # 构造默认文件名
        comp_names = "-".join([c['name'] for c in target_companies_info])
        self.base_default_filename = f"{category}-{comp_names}"
        pos_names = "-".join(target_positions)
        default_name = self.base_default_filename
        if pos_names:
            default_name += f"-{pos_names}"
        default_name += ".xlsx"

        # 2. 其余交互使用 InputManager (为了支持 back 功能)
        im = InputManager()
        # 将已输入的值存入 data，这样 InputManager 运行到这些步骤时会显示默认值为刚输入的内容
        im.data['category'] = category
        im.data['companies'] = companies_str
        im.data['positions'] = positions_str
        
        im.add_step('category', "确认分类", default=category)
        im.add_step('companies', "确认公司配额", default=companies_str)
        im.add_step('positions', "确认关键词 (用'-'分隔)", default=positions_str)
        

        im.add_step('view_phone', "是否需要查看联系方式? (y/N)", default='n')
        im.add_step('format_name', "姓名是否只保留首字母缩写? (y/N)", default='n')
        im.add_step('filename', "请输入输出文件名", default=default_name)
        im.add_step('min_departure', "离职年限不早于 (格式: YY/M 或 'Present')", default="Present")
        im.add_step('earliest_login', "最后一次登陆时间不晚于 (格式: YY/M)", default="")
        im.add_step('zip_id', "请输入压缩包命名标识", default="ZTZ")
        
        self.config = im.run()
        
        # 记录是否使用的是默认生成的名称
        if self.config['filename'] == default_name:
            self.is_default_filename = True
        
        # 重新同步处理后的数据 (处理 InputManager 可能的修改)
        self.target_companies_info = []
        for entry in self.config['companies'].split('/'):
            if not entry.strip(): continue
            parts = entry.strip().rsplit(' ', 1)
            if len(parts) == 2 and parts[1].isdigit():
                self.target_companies_info.append({'name': parts[0].strip(), 'quota': int(parts[1])})
            else:
                self.target_companies_info.append({'name': entry.strip(), 'quota': float('inf')})
        
        self.target_positions = [p.strip() for p in self.config['positions'].split('-') if p.strip()]
        
        # Briefing
        console.print("[yellow]请输入你的访谈提纲 (输入END结束):[/yellow]")
        lines = []
        while True:
            line = input()
            if line.strip().upper() == "END": break
            lines.append(line)
        self.briefing_template = "\n".join(lines)
        
        if not self.briefing_template.strip():
            # If user entered nothing, use a minimal default or just keep it empty? 
            # The user said "needs manual input", so if they leave it empty, it's their choice.
            # But let's check if we should still have a fallback if empty.
            # Actually, the user specifically said "don't have default outline".
            pass
        
        # For display purposes
        target_position_str = "-".join(self.target_positions) if self.target_positions else "不限"
            
        # Filename
        user_filename = self.config['filename']
        if not user_filename.endswith(".xlsx"): user_filename += ".xlsx"
        self.output_filename = os.path.join('data', user_filename)

        # Confirm
        table = Table(title="配置确认")
        table.add_column("配置项", style="cyan")
        table.add_column("值", style="magenta")
        table.add_row("分类", self.config['category'])
        table.add_row("职位", target_position_str)
        table.add_row("输出文件", self.output_filename)
        table.add_row("最早离职", self.config['min_departure'])
        table.add_row("最早登录", self.config['earliest_login'] or "不过滤")
        console.print(table)

    def save_data_to_excel(self):
        # Track old file path for cleanup after successful save
        old_path_to_delete = None
        
        if self.is_default_filename and self.actually_searched_positions:
            pos_part = "-".join(self.actually_searched_positions)
            new_filename = f"{self.base_default_filename}-{pos_part}.xlsx"
            new_full_path = os.path.join('data', new_filename)
            
            if new_full_path != self.output_filename:
                # Remember old file for deletion AFTER successful save
                old_path_to_delete = self.output_filename
                self.output_filename = new_full_path

        with self.contacts_lock:
            if not self.output_filename or not self.saved_contacts:
                console.print("[yellow]--- (保存请求) 没有数据或文件名未设置 ---[/yellow]")
                return
            
            df = pd.DataFrame(list(self.saved_contacts))
            if not df.empty:
                desired_order = ['分类', '公司', '姓名', '在职公司', '职位', '云号码', '在职时间', 'Profile', '简历链接', '是否合作', '最后一次登录时间']
                cols_in_order = [col for col in desired_order if col in df.columns]
                df = df[cols_in_order]
                df.insert(0, '序号', range(1, 1 + len(df)))
                if '序号' in df.columns: df.sort_values(by='序号', ascending=True, inplace=True)
            
            n, m = self.qualified_resumes_count, self.processed_resumes_count

        try:
            # Save new file FIRST (critical: do this before deleting old file)
            df.to_excel(self.output_filename, index=False, engine='openpyxl')
            console.print(f"[green]--- (保存请求) {len(df)} 条数据已成功保存到: {self.output_filename} ---[/green]")
            console.print(f"[bold]--- (保存请求) 当前进度: {n}/{m} (合格/已看) ---[/bold]")
            
            # Only delete old file AFTER successful save
            if old_path_to_delete and os.path.exists(old_path_to_delete):
                try:
                    os.remove(old_path_to_delete)
                    console.print(f"[dim]已清理旧文件: {os.path.basename(old_path_to_delete)}[/dim]")
                except Exception as e:
                    console.print(f"[yellow]无法删除旧文件 {os.path.basename(old_path_to_delete)}: {e}[/yellow]")
                    
        except Exception as e:
            console.print(f"[red]--- (保存请求) 保存到 Excel 时出错: {e} ---[/red]")

    async def run_scraper(self):
        # Setup directories
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
                    TextColumn("[bold cyan]({task.fields[qualified]}/{task.fields[processed]})[/bold cyan]"),
                    TimeElapsedColumn(),
                    console=console
                ) as progress:
                    
                    for company_info in self.target_companies_info:
                        target_company = company_info['name']
                        company_quota = company_info['quota']
                        current_company_qualified_count = 0
                        company_generated_files = []
                        
                        task_id = progress.add_task(
                            f"[cyan]处理公司: {target_company}", 
                            total=company_quota if company_quota != float('inf') else 100,
                            qualified=0,
                            processed=0
                        )
                        
                        briefing_text = self.briefing_template.replace('__COMPANY__', target_company)
                        
                        # Fix: Handle empty position list - default to [""] to search all candidates
                        positions_to_search = self.target_positions if self.target_positions else [""]
                        
                        for current_position in positions_to_search:
                            if current_company_qualified_count >= company_quota: break
                            
                            # Fix: Reset early stopping counter for each new position
                            consecutive_failure_count = 0
                            
                            if current_position not in self.actually_searched_positions:
                                self.actually_searched_positions.append(current_position)

                            position_display = current_position if current_position else "[所有职位]"
                            console.print(f"\n[dim]正在搜索职位: {position_display}[/dim]")
                            await page.goto("https://h.liepin.com/search/getConditionItem")
                            await page.fill('input#rc_select_1, input.search-input, input.company-position-input, .search-box, .search-input', f"{target_company} {current_position}")
                            await page.click('button:has-text("搜 索"), button:has-text("搜索"), .search-btn, .submit-btn')
                            
                            await page.wait_for_load_state('networkidle', timeout=10000)
                            await page.wait_for_timeout(3000)
                            
                            page_number = 1
                            while True:
                                if consecutive_failure_count >= 10: break
                                
                                await page.wait_for_timeout(1000)
                                profile_links_locators = await page.locator(RESUME_LINK_SELECTOR).all()
                                
                                if not profile_links_locators: break
                                
                                for i, link_locator in enumerate(profile_links_locators):
                                    if consecutive_failure_count >= 10: break
                                    
                                    with self.contacts_lock:
                                        self.processed_resumes_count += 1
                                    
                                    # Non-blocking pause check
                                    while not self.pause_flag.is_set():
                                        await asyncio.sleep(0.5)
                                    
                                    profile_page = None
                                    try:
                                        async with context.expect_page() as new_page_info:
                                            await link_locator.click(timeout=5000)
                                        profile_page = await new_page_info.value
                                        await profile_page.wait_for_load_state('domcontentloaded')
                                        await profile_page.wait_for_timeout(2000)
                                        
                                        # --- Validation Logic (Optimized Order) ---
                                        
                                        # 1. Login Date Check
                                        earliest_login_date = parse_login_date_input(self.config['earliest_login'])
                                        actual_login_date_str = "未知"
                                        try:
                                            # 尝试使用更通用的选择器 (Ant Design Tab Extra Content)
                                            login_area_text = await profile_page.locator("#resume-detail-single .ant-tabs-extra-content").text_content(timeout=3000)
                                            match = re.search(r'(\d{4}/\d{2}/\d{2})', login_area_text)
                                            
                                            # 如果上面的失败，尝试在整个头部区域搜索日期模式
                                            if not match:
                                                header_text = await profile_page.locator("#resume-detail-single").text_content(timeout=3000)
                                                # 搜索 "登录" 附近的日期，或者直接搜索日期格式 (假设最近的日期是登录时间)
                                                # 这里假设登录时间通常在顶部，且格式为 YYYY/MM/DD
                                                match = re.search(r'最后登录.*?(\d{4}/\d{2}/\d{2})', header_text)
                                                if not match:
                                                    match = re.search(r'(\d{4}/\d{2}/\d{2})', header_text)

                                            if not match: raise ValueError("无法解析日期")
                                            
                                            actual_login_date_str = match.group(1)
                                            actual_login_date_dt = datetime.strptime(actual_login_date_str, "%Y/%m/%d")
                                            
                                            if earliest_login_date and actual_login_date_dt < earliest_login_date:
                                                console.print(f"[yellow]登录时间不符: {actual_login_date_str} (要求不晚于 {self.config['earliest_login']})[/yellow]")
                                                consecutive_failure_count += 1
                                                progress.update(task_id, processed=self.processed_resumes_count)
                                                continue
                                        except Exception as e:
                                            if earliest_login_date:
                                                console.print(f"[yellow]无法提取登录时间 (选择器可能失效): {e}[/yellow]")
                                                consecutive_failure_count += 1
                                                progress.update(task_id, processed=self.processed_resumes_count)
                                                continue

                                        # 2. Work Time Check
                                        try:
                                            work_time_selector = 'div.work-time, .work-duration, .time-text, .work-time-text, .contact-time, span.rd-work-time'
                                            raw_work_time = await profile_page.locator(work_time_selector).first.text_content(timeout=5000)
                                            work_time = format_work_time(raw_work_time)
                                            if not is_departure_date_ok(work_time, self.config['min_departure']):
                                                console.print(f"[yellow]离职时间不符: {work_time} (要求不早于 {self.config['min_departure']})[/yellow]")
                                                consecutive_failure_count += 1
                                                progress.update(task_id, processed=self.processed_resumes_count)
                                                continue
                                        except Exception as e:
                                            console.print(f"[yellow]无法提取工作时间 (选择器可能失效): {e}[/yellow]")
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue

                                        # 3. Extract Name, Title, Company for field-based checks
                                        name = await profile_page.locator('div.resume-preview-name, .person-name, .resume-name, .name-text, .contact-name, h4.name').first.text_content(timeout=5000)
                                        clean_name = name.strip().replace("*", "")
                                        
                                        gender = ""
                                        try:
                                            info_text = await profile_page.locator('div.basic-cont > div.sep-info').first.inner_text(timeout=5000)
                                            gender = re.search(r'\s*(男|女)\s*', info_text).group(1)
                                        except: pass
                                        
                                        should_format_name = self.config['format_name'].lower() == 'y'
                                        if should_format_name:
                                            clean_name = format_name_to_initials(clean_name, gender)
                                        elif gender and "先生" not in clean_name and "女士" not in clean_name:
                                            clean_name += f"{gender}士" if gender == "女" else "先生"
                                        
                                        title = await profile_page.locator('div.position-name, .work-position, .position-text, .position-title, .contact-position, h6.job-name').first.text_content(timeout=5000)
                                        
                                        company_selector = 'div.company-name, .work-company, .company-text, .company-title, .contact-company, div.rd-work-comp > h5'
                                        company = await profile_page.locator(company_selector).first.text_content(timeout=5000)
                                        
                                        # 4. Company Check (before AI to save API calls)
                                        if target_company.lower() not in company.lower():
                                            console.print(f"[yellow]公司名称不符: {company.strip()} (要求包含 {target_company})[/yellow]")
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue
                                        
                                        # 5. Deduplication Check (before AI to save API calls)
                                        candidate_signature = (extract_name_first_char(clean_name), title.strip(), work_time.strip())
                                        if candidate_signature in self.seen_candidates:
                                            console.print(f"[yellow]发现重复候选人: {clean_name} - {title}，跳过 (节省AI额度)。[/yellow]")
                                            # Note: Do NOT increment consecutive_failure_count for duplicates
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue
                                        
                                        # 6. AI Check (LAST - most expensive operation)
                                        cv_text = await profile_page.locator(CV_TEXT_SELECTOR).text_content(timeout=5000)
                                        match_result = is_match_volc(cv_text, briefing_text)
                                        if match_result is None:
                                            console.print("[yellow]AI API 失败，跳过此候选人[/yellow]")
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue
                                        elif not match_result:
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue
                                        
                                        # --- Success & Extraction ---
                                        summarized_profile = summarize_profile_volc(cv_text, target_company)
                                        # Name/Title/Gender/Company already extracted above
                                        
                                        # --- 先尝试保存 docx，成功后才记录数据 ---
                                        full_html = await profile_page.content()
                                        
                                        # 使用临时序号生成文件名 (基于当前合格数+1)
                                        temp_seq = self.qualified_resumes_count + 1
                                        base_filename = f"{temp_seq}-猎聘-{clean_name}"
                                        docx_filename = os.path.join('resumes', f"{base_filename}.docx")
                                        counter = 1
                                        while os.path.exists(docx_filename):
                                            docx_filename = os.path.join('resumes', f"{base_filename}-{counter}.docx")
                                            counter += 1
                                        
                                        # 尝试保存 docx (带重试机制)
                                        if not save_resume_as_docx(full_html, docx_filename):
                                            console.print(f"[red]--- 由于 docx 保存失败，跳过此候选人: {clean_name} ---[/red]")
                                            consecutive_failure_count += 1
                                            progress.update(task_id, processed=self.processed_resumes_count)
                                            continue
                                        
                                        # --- docx 保存成功，正式记录数据 ---
                                        self.seen_candidates.add(candidate_signature)
                                        company_generated_files.append(docx_filename)
                                        
                                        contact_info = "未查看"
                                        should_view_phone = self.config['view_phone'].lower() == 'y'
                                        if should_view_phone:
                                            contact_info = "需手动查看" 
                                        
                                        with self.contacts_lock:
                                            self.saved_contacts.append({
                                                "分类": self.config['category'],
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
                                            self.qualified_resumes_count += 1
                                            current_company_qualified_count += 1
                                        
                                        consecutive_failure_count = 0
                                        progress.update(task_id, advance=1, qualified=self.qualified_resumes_count, processed=self.processed_resumes_count)
                                        
                                        if current_company_qualified_count >= company_quota:
                                            break

                                    except Exception as e:
                                        console.print(f"[red]处理出错: {e}[/red]")
                                    finally:
                                        if profile_page: await profile_page.close()
                                        # Non-blocking random sleep
                                        await asyncio.sleep(random.uniform(3, 7))
                                
                                if current_company_qualified_count >= company_quota: break
                                
                                # Use simplified selector (tested and verified)
                                next_btn = page.locator("li.ant-pagination-next:not(.ant-pagination-disabled) button")
                                if await next_btn.count() > 0:
                                    await next_btn.click()
                                    await page.wait_for_load_state('networkidle')
                                    page_number += 1
                                else:
                                    break

                        
                        if company_generated_files:
                            zip_identifier = self.config['zip_id']
                            zip_name = os.path.join('zips', f"猎聘-{target_company}-{len(company_generated_files)}份-{zip_identifier}.zip")
                            counter = 1
                            base = zip_name.replace(".zip", "")
                            while os.path.exists(zip_name):
                                zip_name = f"{base}-{counter}.zip"
                                counter += 1
                            zip_company_files(target_company, company_generated_files, zip_name)

            finally:
                self.save_data_to_excel()
                await browser.close()

    def start(self):
        # Keyboard listener thread
        def keyboard_listener():
            try:
                from pynput import keyboard
                def on_press(key):
                    try:
                        if key == keyboard.Key.esc:
                            if self.pause_flag.is_set():
                                console.print("\n[yellow]--- 暂停中... ---[/yellow]")
                                self.pause_flag.clear()
                                self.save_data_to_excel()
                            else:
                                console.print("\n[green]--- 继续运行 ---[/green]")
                                self.pause_flag.set()
                    except AttributeError: pass
                with keyboard.Listener(on_press=on_press) as listener:
                    listener.join()
            except ImportError:
                console.print("pynput未安装")

        listener_thread = threading.Thread(target=keyboard_listener, daemon=True)
        listener_thread.start()
        
        console.rule("[bold blue]猎聘简历自动化助手[/bold blue]")
        self.ensure_browsers_installed()
        
        while True:
            try:
                if Confirm.ask("是否需要重新登录/更新Cookie?"):
                    asyncio.run(self.save_session())
                
                if Confirm.ask("是否清空 data, resumes, zips 文件夹下的所有内容? (y=清空, n=归档)"):
                    self.clear_output_directories()
                else:
                    self.archive_output_directories()
                
                self.load_historical_data()
                self.get_user_inputs()
                
                self.pause_flag.set()
                asyncio.run(self.run_scraper())
            except Exception as e:
                console.print(f"[red]运行出错: {e}[/red]")
            
            if not Confirm.ask("是否开始新一轮搜索?"):
                break

if __name__ == "__main__":
    scraper = LiepinScraper()
    scraper.start()
