# ==============================================================================
# 邮件总结Gradio应用 (app.py)
# ==============================================================================

import os
import imaplib
import email
import re
from email.header import decode_header
from datetime import datetime, timedelta, timezone, date
from bs4 import BeautifulSoup
from google import genai
import gradio as gr

IMAP_SERVER = 'imap.163.com'
EMAIL_ADDRESS = os.environ.get('EMAIL_ADDRESS')
AUTHORIZATION_CODE = os.environ.get('EMAIL_PASSWORD')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

if GEMINI_API_KEY:
    os.environ['GEMINI_API_KEY'] = GEMINI_API_KEY
    print("Gemini API Key 已加载到环境变量。")
else:
    print("警告：未找到GEMINI_API_KEY Secret，AI总结功能将无法使用。")

def get_decoded_header(header_string):
    if header_string is None: return ""
    full_header = []
    for part, charset in decode_header(header_string):
        if isinstance(part, bytes):
            full_header.append(part.decode(charset or 'utf-8', errors='ignore'))
        else:
            full_header.append(str(part))
    return ''.join(full_header)

def fetch_unread_email_dates_and_update_ui(progress=gr.Progress()):
    if not all([EMAIL_ADDRESS, AUTHORIZATION_CODE]):
        error_msg = "错误：请先在Hugging Face Secrets中设置邮箱信息"
        return error_msg, gr.update(choices=[error_msg], value=error_msg), gr.update(), gr.update(), gr.update(interactive=False)

    progress(0, desc="正在连接邮箱...")
    unread_dates = {}
    mail = None
    try:
        local_timezone = timezone(timedelta(hours=8))
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ADDRESS, AUTHORIZATION_CODE)
        mail.xatom('ID', '("name" "my-gradio-client" "version" "1.0")')
        status, _ = mail.select('"Newsletter"', readonly=True)
        if status != 'OK': raise ConnectionError("错误：无法打开'Newsletter'文件夹")
        
        progress(0.3, desc="✅ 连接成功！正在查找未读邮件...")
        status, email_ids_data = mail.search(None, '(UNSEEN)')
        if status != 'OK' or not email_ids_data[0]:
            # 【保持不变】如果没有未读邮件，依然保持按钮可交互
            return "✅ 操作完成。恭喜！没有未读邮件。", gr.update(choices=["没有未读邮件"], value="没有未读邮件", interactive=True), gr.update(interactive=True), gr.update(interactive=True), gr.update(interactive=True)

        email_ids = email_ids_data[0].split()
        progress(0.6, desc=f"✅ 找到 {len(email_ids)} 封未读，正在解析日期...")
        
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, '(BODY[HEADER.FIELDS (Date)])')
            if status == 'OK':
                date_str = get_decoded_header(email.message_from_bytes(msg_data[0][1])['Date'])
                dt_object_with_tz = email.utils.parsedate_to_datetime(date_str)
                if dt_object_with_tz:
                    local_dt_object = dt_object_with_tz.astimezone(local_timezone)
                    d = local_dt_object.date()
                    unread_dates[d] = unread_dates.get(d, 0) + 1
        
        sorted_dates = sorted(unread_dates.items(), key=lambda item: item[0], reverse=True)
        formatted_choices = [f"{dt.strftime('%Y-%m-%d')} ({count}封)" for dt, count in sorted_dates]
        
        # =======================【核心修改】=======================
        # 1. 删除了计算 default_start_dt 和 default_end_dt 的逻辑。
        # 2. 修改了下面的 return 语句。
        # =========================================================
        
        progress(1, desc="✅ 日期解析完毕！")
        # 【修改后的返回语句】
        # - 对 unread_dates_dropdown: 更新选项，但默认值设为 None，不自动选中。
        # - 对 start_date_picker 和 end_date_picker: 只更新为可交互(interactive=True)，不改变它们的值。
        return (
            "✅ 日期解析完毕！请在下方选择日期范围。",
            gr.update(choices=formatted_choices, value=None, interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True)
        )

    except Exception as e:
        error_msg = f"错误: {str(e)}"
        return error_msg, gr.update(choices=[error_msg], value=error_msg), gr.update(), gr.update(), gr.update(interactive=False)
    finally:
        if mail and mail.state == 'SELECTED': mail.close()
        if mail: mail.logout()

def summarize_mail_by_date(start_dt_timestamp, end_dt_timestamp, progress=gr.Progress()):
    """连接邮箱，获取并总结指定日期范围内的邮件。"""
    
    # 【核心修正】: 将Gradio传入的float时间戳转换为datetime对象
    try:
        start_dt = datetime.fromtimestamp(start_dt_timestamp)
        end_dt = datetime.fromtimestamp(end_dt_timestamp)
    except TypeError:
        # 如果传入的已经是datetime对象，则直接使用（增加代码兼容性）
        start_dt = start_dt_timestamp
        end_dt = end_dt_timestamp

    # 检查Secrets是否都已正确配置
    if not all([EMAIL_ADDRESS, AUTHORIZATION_CODE, GEMINI_API_KEY]):
        yield "❌ 错误：启动失败！请检查所有Secrets是否都已正确配置。", ""
        return

    # 从datetime对象中提取date对象 (现在这部分可以正常工作了)
    start_date = start_dt.date()
    end_date = end_dt.date()

    TARGET_MAILBOX = "Newsletter" 
    all_summaries_html = ""
    mail = None 
    try:
        yield "正在连接到IMAP服务器...", all_summaries_html
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ADDRESS, AUTHORIZATION_CODE)
        mail.xatom('ID', '("name" "my-gradio-client" "version" "1.0")')
        yield "✅ 登录成功，正在选择文件夹...", all_summaries_html
        
        status, _ = mail.select(f'"{TARGET_MAILBOX}"')
        if status != 'OK':
            yield f"❌ 错误：无法选择文件夹 '{TARGET_MAILBOX}'。", ""
            return

        since_formatted = start_date.strftime("%d-%b-%Y")
        before_formatted = (end_date + timedelta(days=1)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE {since_formatted} BEFORE {before_formatted})'
        yield f"正在搜索从 {start_date.strftime('%Y-%m-%d')} 到 {end_date.strftime('%Y-%m-%d')} 的邮件...", all_summaries_html
        
        status, email_ids_data = mail.search(None, search_criteria)
        if status != 'OK' or not email_ids_data[0]:
            yield "✅ 搜索完成！在指定日期范围内没有收到任何邮件。", ""
            return
        
        email_ids = email_ids_data[0].split()
        total_emails = len(email_ids)
        yield f"✅ 找到 {total_emails} 封邮件，准备开始处理...", all_summaries_html

        for i, email_id in enumerate(email_ids):
            progress(i / total_emails, desc=f"正在处理第 {i+1}/{total_emails} 封邮件")
            status, msg_data = mail.fetch(email_id, '(RFC822)')
            if status != 'OK': continue
            
            msg = email.message_from_bytes(msg_data[0][1])
            subject = get_decoded_header(msg['Subject'])
            sender = get_decoded_header(msg['From'])
            date_str = get_decoded_header(msg['Date'])
            text_content = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    html_body = payload.decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    soup = BeautifulSoup(html_body, 'html.parser')
                    text_content = soup.get_text(separator='\n', strip=True)
                    break
            
            if not text_content: continue
            
            yield f"正在为邮件“{subject[:20]}...”调用AI总结...", all_summaries_html
            try:
                client = genai.Client()
                prompt = f"请用中文全面、详细地总结以下邮件内容（不用Markdown回复）:\n\n---\n\n{text_content[:8000]}"
                response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                summary = response.text
                
                all_summaries_html += f"<h3>{i+1}. {subject}</h3>"
                all_summaries_html += f"<p style='color: #555; font-size: 0.9em; margin-top:-10px;'><b>发件人:</b> {sender}<br><b>日期:</b> {date_str}</p>"
                all_summaries_html += f"<div style='border-left: 4px solid #4CAF50; padding-left: 1em; white-space: pre-wrap;'>{summary}</div><hr>"
                yield f"第 {i+1}/{total_emails} 封邮件总结完毕...", all_summaries_html
            except Exception as e:
                all_summaries_html += f"<h3>{i+1}. {subject}</h3><p>❌ 调用 Gemini API 失败: {e}</p><hr>"
                yield f"第 {i+1}/{total_emails} 封邮件AI总结失败。", all_summaries_html
        
        yield f"🎉 全部 {total_emails} 封邮件处理完毕！", all_summaries_html
    except Exception as e:
        yield f"发生严重错误: {e}", ""
    finally:
        if mail: mail.logout()

with gr.Blocks(theme=gr.themes.Soft(primary_hue="teal"), title="邮件智能总结助手") as demo:
    gr.Markdown("# 📧 邮件智能总结助手")
    gr.Markdown("自动连接到您的“Newsletter”文件夹，一键总结指定日期的所有邮件。")

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### 1. 连接邮箱")
            connect_button = gr.Button("🔗 连接邮箱并查找未读邮件", variant="secondary")
            
            gr.Markdown("### 2. 选择日期范围")
            unread_dates_dropdown = gr.Dropdown(label="高亮的未读邮件日期 (可选)", choices=[], info="选择后会自动填充下面的日期。", interactive=False)
            # 默认填充昨天的日期
            yesterday_obj = date.today() - timedelta(days=1)
            default_start_datetime = datetime.combine(yesterday_obj, datetime.min.time())
            default_end_datetime = datetime.combine(yesterday_obj, datetime.max.time())
            
            start_date_picker = gr.DateTime(label="开始日期", value=default_start_datetime, interactive=True)
            end_date_picker = gr.DateTime(label="结束日期", value=default_end_datetime, interactive=True)
            
            gr.Markdown("### 3. 开始总结")
            run_button = gr.Button("🚀 对选定日期范围进行总结", variant="primary", interactive=False)
            
            progress_output = gr.Textbox(label="实时日志", lines=10, interactive=False)

        with gr.Column(scale=2):
            gr.Markdown("### ✨ AI 总结结果")
            summary_output = gr.HTML()

    def update_date_pickers_from_dropdown(selected_date_str):
        if selected_date_str and " (" in selected_date_str:
            date_str = selected_date_str.split(" (")[0]
            selected_date = datetime.strptime(date_str, "%Y-%m-%d")
            start_dt = datetime.combine(selected_date, datetime.min.time())
            end_dt = datetime.combine(selected_date, datetime.max.time())
            return start_dt, end_dt
        return gr.skip(), gr.skip()
        
    connect_button.click(
        fn=fetch_unread_email_dates_and_update_ui,
        inputs=[],
        outputs=[progress_output, unread_dates_dropdown, start_date_picker, end_date_picker, run_button]
    )

    unread_dates_dropdown.change(
        fn=update_date_pickers_from_dropdown,
        inputs=[unread_dates_dropdown],
        outputs=[start_date_picker, end_date_picker]
    )

    run_button.click(
        fn=summarize_mail_by_date,
        inputs=[start_date_picker, end_date_picker],
        outputs=[progress_output, summary_output]
    )

if __name__ == "__main__":
    # 监听在0.0.0.0，允许来自容器外部的连接
    demo.launch(server_name="0.0.0.0", server_port=10000)
