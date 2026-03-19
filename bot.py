import requests
from bs4 import BeautifulSoup
import os
import sys
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ================= 설정 부분 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LAST_NUM_FILE = "last_num.txt"
CHAT_IDS_FILE = "chat_ids.txt"
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

def send_telegram_message(text, chat_ids):
    """여러 사용자에게 메시지를 전송합니다."""
    for chat_id in chat_ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
        try:
            requests.post(url, data=payload)
        except Exception as e:
            print(f"Chat ID {chat_id} 전송 실패: {e}")

def get_latest_notices():
    """Selenium을 사용하여 자바스크립트 보안을 통과한 후 최신 글을 가져옵니다."""
    url = "https://cse.knu.ac.kr/bbs/board.php?bo_table=sub5_1"
    latest_posts = []
    
    # Chrome 브라우저를 화면 없이(Headless) 실행하기 위한 설정
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    driver = None
    try:
        # 웹 브라우저 실행
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(url)
        
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
            title = title_element.text.strip()
            link = title_element['href']
            
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
    """새로운 글 확인 및 알림 로직 (기존과 동일)"""
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
            message = f"📢 <b>새로운 공지사항이 등록되었습니다!</b>\n\n"
            message += f"▪️ <b>제목:</b> {post['title']}\n"
            message += f"▪️ <b>링크:</b> <a href='{post['link']}'>바로가기</a>"
            
            if chat_ids:
                send_telegram_message(message, chat_ids)
                print(f"알림 전송 완료: {post['number']}번 글")
            
            new_last_num = max(new_last_num, post['number'])

    if new_last_num > last_num or last_num == 0:
        with open(LAST_NUM_FILE, 'w', encoding='utf-8') as f:
            f.write(str(new_last_num))
        print(f"마지막 글 번호 업데이트 완료: {new_last_num}")
    else:
        print(f"새로운 공지사항이 없습니다. (마지막 글 번호: {last_num})")

if __name__ == "__main__":
    print("공지사항 크롤링 및 구독자 알림을 시작합니다...")
    check_new_notices()