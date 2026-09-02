"""경북대학교 컴퓨터학부 공지 게시판을 크롤링하고 Telegram 서식으로 변환합니다."""

import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from config import (
    BOARDS,
    TELEGRAM_MARKDOWN_BLOCK_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    USER_AGENT,
)


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


def build_notice_title(post):
    """게시판 이름을 앞에 붙인 알림용 제목을 만듭니다."""
    title = post["title"]
    if post.get("board_name"):
        title = f"[{post['board_name']}] {title}"
    return title


def pack_blocks_into_messages(blocks):
    """서식 블록을 텔레그램 한 통 길이에 맞게 묶습니다."""
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


def build_notice_messages(post):
    """제목, 서식이 유지된 본문, 원문 링크를 제한 길이에 맞춰 묶습니다."""
    body_blocks = post.get("content_markdown_blocks")
    if body_blocks is None:
        body = post.get("content", "").strip() or "(본문 내용 없음)"
        body_blocks = plain_text_to_markdown_blocks(body)

    return pack_blocks_into_messages([
        f"📢 *{escape_markdown_v2(build_notice_title(post))}*",
        *body_blocks,
        f"🔗 [원문 보기]({escape_markdown_url(post['link'])})",
    ])


def build_headline_messages(post):
    """본문을 받지 않는 사용자를 위해 게시판·제목·링크만 담습니다."""
    return pack_blocks_into_messages([
        f"📢 *{escape_markdown_v2(build_notice_title(post))}*",
        f"🔗 [원문 보기]({escape_markdown_url(post['link'])})",
    ])


def safe_filename(name, default="attachment"):
    """운영체제에서 사용할 수 없는 문자를 제거한 안전한 파일명을 반환합니다."""
    name = unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180] or default


def refresh_download_url(file_info, page_html, referer):
    """현재 세션의 상세 페이지에서 같은 첨부파일의 새 nonce URL을 찾습니다."""
    original_url = file_info["url"]
    original_parsed = urlparse(original_url)
    if not original_parsed.path.endswith("/bbs/download.php"):
        return original_url

    original_query = parse_qs(original_parsed.query)
    identity_keys = ("bo_table", "wr_id", "no")
    if not all(original_query.get(key) for key in identity_keys):
        return original_url

    soup = BeautifulSoup(page_html, "html.parser")
    for link in soup.select("#bo_v_file a[href]"):
        candidate_url = urljoin(referer, link.get("href"))
        candidate_parsed = urlparse(candidate_url)
        if not candidate_parsed.path.endswith("/bbs/download.php"):
            continue
        candidate_query = parse_qs(candidate_parsed.query)
        if all(
            original_query.get(key) == candidate_query.get(key)
            for key in identity_keys
        ):
            return candidate_url

    return original_url


def download_file(file_info, directory, referer):
    """게시판 파일을 임시 폴더에 다운로드합니다."""
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    # 그누보드 다운로드는 상세 페이지에서 발급한 PHP 세션이 없으면
    # HTTP 200의 오류 HTML을 반환하므로 같은 세션으로 상세 페이지를 먼저 엽니다.
    with requests.Session() as session:
        session.headers.update(headers)
        with session.get(referer, timeout=30) as page_response:
            page_response.raise_for_status()
            download_url = refresh_download_url(
                file_info,
                page_response.text,
                referer,
            )

        # 외부 이미지 서버가 응답하지 않아도 다음 공지 알림까지 장시간 지연되지 않게 합니다.
        with session.get(
            download_url,
            stream=True,
            timeout=(10, 60),
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise RuntimeError(
                    "사이트가 첨부파일 대신 오류 페이지를 반환했습니다."
                )

            filename = safe_filename(file_info.get("name"))
            path = Path(directory) / filename
            counter = 1
            while path.exists():
                path = (
                    Path(directory)
                    / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                )
                counter += 1

            with path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
    return path


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
    content_element = soup.select_one(
        "#bo_v_con, #bo_v_atc .view_content, .bo_v_con"
    )
    if not content_element:
        raise ValueError("상세 페이지에서 본문을 찾지 못했습니다.")

    attachments = []
    seen_attachment_urls = set()
    for link in soup.select(
        "#bo_v_file a.view_file_download[href], "
        "#bo_v_file a[href*='download.php']"
    ):
        url = urljoin(page_url, link.get("href"))
        if url in seen_attachment_urls:
            continue
        seen_attachment_urls.add(url)
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


def get_notice_details(page_url, board_url=None):
    """공지 상세 페이지를 가져와 본문과 파일 정보를 반환합니다."""
    response = requests.get(
        page_url,
        headers={"User-Agent": USER_AGENT, "Referer": board_url or page_url},
        timeout=30,
    )
    response.raise_for_status()
    return parse_notice_details(response.text, page_url)


def parse_notice_list(html, board):
    """새 홈페이지의 공지 목록 HTML에서 글 번호, 제목, 링크를 추출합니다."""
    soup = BeautifulSoup(html, "html.parser")
    notice_table = soup.select_one(
        "#bo_list #fboardlist .basic_tbl_head table, "
        "#bo_list .basic_tbl_head table"
    )
    if not notice_table:
        return []

    latest_posts = []
    seen_post_ids = set()
    for row in notice_table.select("tbody tr"):
        if not row.select_one(".td_num2"):
            continue

        title_element = row.select_one(".td_subject .bo_tit a[href]")
        if not title_element:
            continue

        link = urljoin(board["url"], title_element.get("href", ""))
        post_id_values = parse_qs(urlparse(link).query).get("wr_id")
        if not post_id_values:
            continue
        try:
            real_post_id = int(post_id_values[0])
        except (TypeError, ValueError):
            continue
        if real_post_id in seen_post_ids:
            continue
        seen_post_ids.add(real_post_id)

        latest_posts.append({
            "number": real_post_id,
            "title": title_element.get_text(" ", strip=True),
            "link": link,
            "board_key": board["key"],
            "board_name": board["name"],
            "board_url": board["url"],
        })
    return latest_posts


def get_latest_notices():
    """설정된 각 게시판에서 최신 글 목록을 가져옵니다."""
    latest_posts_by_board = {}
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        for board in BOARDS:
            try:
                response = session.get(board["url"], timeout=30)
                response.raise_for_status()
                posts = parse_notice_list(response.text, board)
                if not posts:
                    print(
                        f"❗ {board['name']} 게시판에서 게시글을 찾지 못했습니다."
                    )
                    continue
                latest_posts_by_board[board["key"]] = posts
            except Exception as error:
                print(f"{board['name']} 게시판 크롤링 중 오류 발생: {error}")
    finally:
        session.close()

    return latest_posts_by_board
