import requests
from bs4 import BeautifulSoup, NavigableString, Tag
import os
import sys
import time
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ================= 설정 부분 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LAST_NUM_FILE = "last_num.txt"
CHAT_IDS_FILE = "chat_ids.txt"
BOARD_URL = "https://cse.knu.ac.kr/bbs/board.php?bo_table=sub5_1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_MARKDOWN_BLOCK_LIMIT = 3500
# =============================================

def update_subscribers():
    """새로 봇에게 말을 건 사용자의 Chat ID를 수집하여 파일에 저장합니다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    subscribers = set()
    if os.path.exists(CHAT_IDS_FILE):
        with open(CHAT_IDS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    subscribers.add(stripped)
    try:
        response = requests.get(url)
        data = response.json()
        if data.get("ok"):
            for result in data.get("result", []):
                if "message" in result and "chat" in result["message"]:
                    chat_id = str(result["message"]["chat"]["id"])
                    subscribers.add(chat_id)
                    
        with open(CHAT_IDS_FILE, 'w', encoding='utf-8') as f:
            for chat_id in subscribers:
                f.write(chat_id + "\n")
        return list(subscribers)
    except Exception as e:
        print(f"구독자 업데이트 중 오류 발생: {e}")
        return list(subscribers)

def telegram_api_request(method, chat_id, data=None, files=None):
    """텔레그램 API를 호출하고 실패 시 예외를 발생시킵니다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    payload = {"chat_id": chat_id}
    if data:
        payload.update(data)

    response = requests.post(url, data=payload, files=files, timeout=60)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "텔레그램 API 오류"))
    return result


def split_message(text, limit=TELEGRAM_MESSAGE_LIMIT):
    """텔레그램 글자 수 제한에 맞춰 문단/단어 경계에서 메시지를 나눕니다."""
    text = text.strip()
    chunks = []

    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            word_boundary = text.rfind(" ", 0, limit + 1)
            split_at = word_boundary if word_boundary >= limit // 2 else limit
        if split_at <= 0:
            split_at = limit

        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)
    return chunks


def escape_markdown_v2(text):
    """텔레그램 MarkdownV2에서 특별한 의미를 갖는 문자를 이스케이프합니다."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def escape_markdown_url(url):
    """Markdown 링크 URL 내부에서 필요한 문자만 이스케이프합니다."""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")


def wrap_markdown(text, marker):
    """앞뒤 공백은 서식 밖에 두어 Markdown 파싱 오류를 방지합니다."""
    if not text or not text.strip():
        return text
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    core = text.strip()
    return f"{leading}{marker}{core}{marker}{trailing}"


def get_markdown_styles(node):
    """HTML 태그와 인라인 CSS에서 Telegram이 지원하는 서식을 찾습니다."""
    styles = set()
    name = node.name.lower()
    if name in {"strong", "b", "h1", "h2", "h3", "h4", "h5", "h6"}:
        styles.add("bold")
    if name in {"em", "i"}:
        styles.add("italic")
    if name == "u":
        styles.add("underline")
    if name in {"s", "strike", "del"}:
        styles.add("strikethrough")

    style = re.sub(r"\s+", "", node.get("style", "").lower())
    if "font-style:italic" in style:
        styles.add("italic")
    if (
        "font-weight:bold" in style
        or re.search(r"font-weight:[6-9]00", style)
    ):
        styles.add("bold")
    if (
        "text-decoration:underline" in style
        or "text-decoration-line:underline" in style
    ):
        styles.add("underline")
    if (
        "text-decoration:line-through" in style
        or "text-decoration-line:line-through" in style
    ):
        styles.add("strikethrough")
    return styles


def apply_markdown_styles(text, styles):
    """서식 마커를 항상 같은 순서로 적용해 중첩을 안정적으로 만듭니다."""
    markers = (
        ("italic", "_"),
        ("bold", "*"),
        ("underline", "__"),
        ("strikethrough", "~"),
    )
    for style, marker in markers:
        if style in styles:
            text = wrap_markdown(text, marker)
    return text


def get_display_width(text):
    """고정폭 글꼴에서 한글과 영문이 차지하는 표시 폭을 계산합니다."""
    width = 0
    for character in str(text):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def pad_display_text(text, width):
    """한글의 2칸 폭을 고려해 표 셀 오른쪽을 공백으로 채웁니다."""
    return str(text) + " " * max(0, width - get_display_width(text))


def extract_table_matrix(table):
    """rowspan과 colspan을 펼쳐 HTML 표를 직사각형 텍스트 행렬로 만듭니다."""
    rows = [
        row for row in table.find_all("tr")
        if row.find_parent("table") is table
    ]
    matrix = []

    def ensure_cell(row_index, column_index):
        while len(matrix) <= row_index:
            matrix.append([])
        while len(matrix[row_index]) <= column_index:
            matrix[row_index].append(None)

    for row_index, row in enumerate(rows):
        ensure_cell(row_index, 0)
        column_index = 0
        cells = row.find_all(["th", "td"], recursive=False)
        for cell in cells:
            while (
                column_index < len(matrix[row_index])
                and matrix[row_index][column_index] is not None
            ):
                column_index += 1

            text = re.sub(r"\s+", " ", " ".join(cell.stripped_strings)).strip()
            try:
                rowspan = max(1, int(cell.get("rowspan", 1)))
            except (TypeError, ValueError):
                rowspan = 1
            try:
                colspan = max(1, int(cell.get("colspan", 1)))
            except (TypeError, ValueError):
                colspan = 1

            for target_row in range(row_index, row_index + rowspan):
                for target_column in range(
                    column_index, column_index + colspan
                ):
                    ensure_cell(target_row, target_column)
                    matrix[target_row][target_column] = text
            column_index += colspan

    column_count = max((len(row) for row in matrix), default=0)
    return [
        [cell or "" for cell in row + [None] * (column_count - len(row))]
        for row in matrix
    ]


def render_table_markdown(table):
    """HTML 표를 텔레그램에서 가로 스크롤 가능한 고정폭 표로 만듭니다."""
    matrix = extract_table_matrix(table)
    if not matrix:
        return ""

    column_widths = [
        max(get_display_width(row[column]) for row in matrix)
        for column in range(len(matrix[0]))
    ]

    lines = []
    for row_index, row in enumerate(matrix):
        lines.append(
            " | ".join(
                pad_display_text(cell, column_widths[column])
                for column, cell in enumerate(row)
            )
        )
        if row_index == 0 and len(matrix) > 1:
            lines.append("-+-".join("-" * width for width in column_widths))

    table_text = "\n".join(lines)
    table_text = table_text.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{table_text}\n```\n\n"


def render_telegram_markdown(node, base_url="", active_styles=frozenset()):
    """게시글 HTML 노드를 Telegram MarkdownV2 문자열로 변환합니다."""
    if isinstance(node, NavigableString):
        text = str(node).replace("\xa0", " ")
        return escape_markdown_v2(re.sub(r"\s+", " ", text))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in {"script", "style", "img"}:
        return ""
    if name == "br":
        return "\n"
    if name == "hr":
        return "──────────\n\n"
    if name == "pre":
        code = node.get_text().replace("\\", "\\\\").replace("`", "\\`").strip()
        return f"```\n{code}\n```\n\n"
    if name == "code":
        code = node.get_text().replace("\\", "\\\\").replace("`", "\\`")
        return f"`{code}`"
    if name == "table":
        return render_table_markdown(node)

    if name == "tr":
        cells = [
            render_telegram_markdown(cell, base_url, active_styles).strip()
            for cell in node.find_all(["th", "td"], recursive=False)
        ]
        return " │ ".join(cell for cell in cells if cell) + "\n\n"

    node_styles = get_markdown_styles(node)
    new_styles = node_styles - active_styles
    child_styles = active_styles | node_styles
    children = "".join(
        render_telegram_markdown(child, base_url, child_styles)
        for child in node.children
    )

    if name == "a":
        href = node.get("href", "").strip()
        if href and not href.startswith(("#", "javascript:")):
            label = children.strip() or escape_markdown_v2(href)
            children = f"[{label}]({escape_markdown_url(urljoin(base_url, href))})"

    children = apply_markdown_styles(children, new_styles)

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"{children.strip()}\n\n"
    if name == "blockquote":
        quoted = "\n".join(f">{line}" for line in children.strip().splitlines())
        return f"{quoted}\n\n"
    if name == "li":
        return f"• {children.strip()}\n\n"
    if name in {"p", "div", "section", "article"}:
        return f"{children.strip()}\n\n" if children.strip() else ""
    if name in {"table", "thead", "tbody", "tfoot", "ul", "ol"}:
        return children

    return children


def markdown_v2_to_plain(text):
    """전송 실패 시 사용할 수 있도록 MarkdownV2 문법을 일반 텍스트로 되돌립니다."""
    text = re.sub(r"(?m)^>", "", text)
    text = re.sub(r"(?<!\\)(?:```|[*_~`])", "", text)
    return re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', text)


def plain_text_to_markdown_blocks(text, limit=TELEGRAM_MARKDOWN_BLOCK_LIMIT):
    """긴 일반 텍스트를 이스케이프된 Markdown 블록들로 변환합니다."""
    pending = split_message(text, max(1, limit // 2))
    blocks = []
    for piece in pending:
        escaped = escape_markdown_v2(piece)
        if len(escaped) <= limit:
            blocks.append(escaped)
            continue
        midpoint = max(1, len(piece) // 2)
        blocks.extend(plain_text_to_markdown_blocks(piece[:midpoint], limit))
        blocks.extend(plain_text_to_markdown_blocks(piece[midpoint:], limit))
    return blocks


def html_content_to_markdown_blocks(content_element, base_url=""):
    """HTML 본문을 서식 경계가 보존된 Telegram MarkdownV2 블록으로 변환합니다."""
    if not content_element:
        return []

    content = BeautifulSoup(str(content_element), "html.parser")
    rendered = render_telegram_markdown(content, base_url)
    # 같은 서식의 인접 span들이 각각 닫히고 열리며 생긴 빈 마커를 하나로 합칩니다.
    for _ in range(3):
        rendered = re.sub(r"(?<!\\)_{4}", "", rendered)
        rendered = re.sub(r"(?<!\\)\*{2}", "", rendered)
        rendered = re.sub(r"(?<!\\)~{2}", "", rendered)
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()

    blocks = []
    for block in rendered.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if len(block) <= TELEGRAM_MARKDOWN_BLOCK_LIMIT:
            blocks.append(block)
        else:
            blocks.extend(plain_text_to_markdown_blocks(markdown_v2_to_plain(block)))
    return blocks


def build_notice_messages(post):
    """제목, 서식이 유지된 본문, 원문 링크를 제한 길이에 맞춰 묶습니다."""
    body_blocks = post.get("content_markdown_blocks")
    if body_blocks is None:
        body = post.get("content", "").strip() or "(본문 내용 없음)"
        body_blocks = plain_text_to_markdown_blocks(body)

    blocks = [
        f"📢 *{escape_markdown_v2(post['title'])}*",
        *body_blocks,
        f"🔗 [원문 보기]({escape_markdown_url(post['link'])})",
    ]
    messages = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def send_telegram_message(text, chat_ids):
    """여러 사용자에게 MarkdownV2 메시지를 전송합니다."""
    succeeded = True
    for chat_id in chat_ids:
        try:
            telegram_api_request(
                "sendMessage",
                chat_id,
                {"text": text, "parse_mode": "MarkdownV2"},
            )
        except Exception as e:
            print(f"Chat ID {chat_id} 전송 실패: {e}")
            succeeded = False
    return succeeded


def safe_filename(name, default="attachment"):
    """운영체제에서 사용할 수 없는 문자를 제거한 안전한 파일명을 반환합니다."""
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180] or default


def download_file(file_info, directory, referer):
    """게시판 파일을 임시 폴더에 다운로드합니다."""
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    # 그누보드 다운로드는 상세 페이지에서 발급한 PHP 세션이 없으면
    # HTTP 200의 오류 HTML을 반환하므로 같은 세션으로 상세 페이지를 먼저 엽니다.
    session = requests.Session()
    session.headers.update(headers)
    page_response = session.get(referer, timeout=30)
    page_response.raise_for_status()
    response = session.get(file_info["url"], stream=True, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        response.close()
        raise RuntimeError("사이트가 첨부파일 대신 오류 페이지를 반환했습니다.")

    filename = safe_filename(file_info.get("name"))
    path = Path(directory) / filename
    counter = 1
    while path.exists():
        path = Path(directory) / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1

    with path.open("wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                output.write(chunk)
    return path


def send_telegram_file(file_info, chat_ids, referer, label="첨부파일"):
    """파일을 한 번 내려받은 뒤 모든 구독자에게 문서로 전송합니다."""
    succeeded = True
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = download_file(file_info, directory, referer)
            for chat_id in chat_ids:
                try:
                    with path.open("rb") as document:
                        telegram_api_request(
                            "sendDocument",
                            chat_id,
                            {"caption": f"📎 {label}: {file_info['name']}"},
                            {"document": (path.name, document)},
                        )
                except Exception as e:
                    print(f"Chat ID {chat_id} 파일 전송 실패 ({file_info['name']}): {e}")
                    succeeded = False
    except Exception as e:
        print(f"파일 다운로드 실패 ({file_info['name']}): {e}")
        succeeded = False
    return succeeded


def html_content_to_text(content_element, base_url=""):
    """게시글 HTML을 읽기 쉬운 일반 텍스트로 변환합니다."""
    if not content_element:
        return ""

    content = BeautifulSoup(str(content_element), "html.parser")
    for unwanted in content.select("script, style"):
        unwanted.decompose()
    for link in content.select("a[href]"):
        href = link.get("href", "").strip()
        absolute_url = urljoin(base_url, href)
        if href and not href.startswith(("#", "javascript:")) and absolute_url not in link.get_text():
            link.append(f" ({absolute_url})")
    for line_break in content.select("br"):
        line_break.replace_with("\n")
    for cell in content.select("td, th"):
        cell.append("\t")
    for block in content.select("p, div, li, tr, h1, h2, h3, h4, h5, h6, blockquote"):
        block.append("\n")

    text = content.get_text()
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_notice_details(html, page_url):
    """상세 페이지에서 본문, 첨부파일, 본문 이미지를 추출합니다."""
    soup = BeautifulSoup(html, "html.parser")
    content_element = soup.select_one("#bo_v_con")
    if not content_element:
        raise ValueError("상세 페이지에서 본문을 찾지 못했습니다.")

    attachments = []
    for link in soup.select("#bo_v_file a[href]"):
        url = urljoin(page_url, link.get("href"))
        name_element = link.select_one("strong")
        name = name_element.get_text(" ", strip=True) if name_element else link.get_text(" ", strip=True)
        attachments.append({"name": safe_filename(name), "url": url})

    inline_images = []
    seen_urls = set()
    for index, image in enumerate(content_element.select("img"), start=1):
        source = image.get("data-src") or image.get("src")
        if not source or source.startswith("data:"):
            continue
        url = urljoin(page_url, source)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        name = image.get("alt", "").strip() or Path(urlparse(url).path).name
        if not Path(name).suffix:
            name = f"본문_이미지_{index}.jpg"
        inline_images.append({"name": safe_filename(name, f"본문_이미지_{index}.jpg"), "url": url})

    return {
        "content": html_content_to_text(content_element, page_url),
        "content_markdown_blocks": html_content_to_markdown_blocks(
            content_element, page_url
        ),
        "attachments": attachments,
        "inline_images": inline_images,
    }


def get_notice_details(page_url):
    """공지 상세 페이지를 가져와 본문과 파일 정보를 반환합니다."""
    response = requests.get(
        page_url,
        headers={"User-Agent": USER_AGENT, "Referer": BOARD_URL},
        timeout=30,
    )
    response.raise_for_status()
    return parse_notice_details(response.text, page_url)

def get_latest_notices():
    """Selenium을 사용하여 자바스크립트 보안을 통과한 후 최신 글을 가져옵니다."""
    latest_posts = []
    
    # Chrome 브라우저를 화면 없이(Headless) 실행하기 위한 설정
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(f"user-agent={USER_AGENT}")
    
    driver = None
    try:
        # 웹 브라우저 실행
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(BOARD_URL)
        
        # 브라우저가 자바스크립트 챌린지를 풀고 실제 페이지를 로딩할 때까지 5초간 대기합니다.
        time.sleep(5)
        
        # 렌더링이 끝난 페이지의 HTML 코드를 가져옵니다.
        html = driver.page_source
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.select('tbody tr')
        
        if not rows:
            print("❗ 대기 후에도 게시글을 찾지 못했습니다. 방화벽이 더 강력하게 차단했을 수 있습니다.")
            
        for row in rows:
            num_element = row.select_one('.td_num2')
            if not num_element:
                continue
            num_text = num_element.text.strip()
            # if num_text == "공지":
            #     continue
                
            title_element = row.select_one('.td_subject .bo_tit a')
            if not title_element:
                continue
            title = title_element.text.strip()
            link = urljoin(BOARD_URL, title_element['href'])
            
            try:
                real_post_id = int(link.split('wr_id=')[1].split('&')[0])
            except ValueError:
                continue
            
            latest_posts.append({
                'number': real_post_id,
                'title': title,
                'link': link
            })
    except Exception as e:
        print(f"크롤링 중 오류 발생: {e}")
    finally:
        # 작업이 끝나면 반드시 브라우저를 종료해야 메모리 누수가 발생하지 않습니다.
        if driver:
            driver.quit()
            
    return latest_posts

def check_new_notices():
    """새 글의 제목, 본문, 본문 이미지, 첨부파일을 구독자에게 전송합니다."""
    if not TELEGRAM_TOKEN:
        print("오류: 환경변수에 TELEGRAM_TOKEN이 설정되지 않았습니다.")
        sys.exit(1)

    chat_ids = update_subscribers()
    if not chat_ids:
        print("등록된 구독자가 없습니다.")

    last_num = 0
    if os.path.exists(LAST_NUM_FILE):
        with open(LAST_NUM_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if content.isdigit():
                last_num = int(content)

    posts = get_latest_notices()
    if not posts:
        print("게시글을 불러오지 못했습니다.")
        return

    posts.reverse()
    new_last_num = last_num

    for post in posts:
        if last_num == 0:
            new_last_num = max(new_last_num, post['number'])
            continue

        if post['number'] > last_num:
            try:
                post.update(get_notice_details(post["link"]))
            except Exception as e:
                print(f"{post['number']}번 글 상세 내용 조회 실패: {e}")
                # 이후 글 번호로 건너뛰면 실패한 공지를 영영 놓칠 수 있으므로 다음 실행에서 재시도합니다.
                break

            delivered = True
            if chat_ids:
                for message in build_notice_messages(post):
                    delivered = send_telegram_message(message, chat_ids) and delivered
                for image in post["inline_images"]:
                    delivered = send_telegram_file(
                        image, chat_ids, post["link"], label="본문 이미지"
                    ) and delivered
                for attachment in post["attachments"]:
                    delivered = send_telegram_file(
                        attachment, chat_ids, post["link"]
                    ) and delivered

                if delivered:
                    print(f"알림 전송 완료: {post['number']}번 글")
                else:
                    print(f"일부 내용 전송 실패: {post['number']}번 글")

            if delivered:
                new_last_num = max(new_last_num, post['number'])
            else:
                # 번호를 갱신하지 않고 다음 실행에서 이 공지부터 다시 전송합니다.
                break

    if new_last_num > last_num or last_num == 0:
        with open(LAST_NUM_FILE, 'w', encoding='utf-8') as f:
            f.write(str(new_last_num))
        print(f"마지막 글 번호 업데이트 완료: {new_last_num}")
    else:
        print(f"새로운 공지사항이 없습니다. (마지막 글 번호: {last_num})")

if __name__ == "__main__":
    print("공지사항 크롤링 및 구독자 알림을 시작합니다...")
    check_new_notices()
