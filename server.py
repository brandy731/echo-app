#!/usr/bin/env python3
"""回响 Echo · 后端服务器
代理网易云公开 API 请求（绕过浏览器 CORS），
并对拉取到的数据进行聚合分析。
"""

import json
import urllib.request
import urllib.error
import time
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 9556))
NETEASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15',
    'Referer': 'https://music.163.com'
}

# ============================================================
# 网易云 API 数据拉取
# ============================================================

def fetch_json(url, headers=NETEASE_HEADERS, timeout=15):
    """安全的 HTTP GET → JSON"""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def fetch_all_time_records(uid):
    """全历史听歌排行（100首）"""
    try:
        data = fetch_json(f'https://music.163.com/api/v1/play/record?uid={uid}&type=0')
        records = data.get('allData', []) or []
        return [_format_track(item) for item in records[:100]]
    except Exception as e:
        print(f'[ERR] allTime records: {e}')
        return []

def fetch_recent_records(uid):
    """近期播放排行（100首）"""
    try:
        data = fetch_json(f'https://music.163.com/api/v1/play/record?uid={uid}&type=1')
        records = data.get('weekData', []) or []
        return [_format_track(item) for item in records[:100]]
    except Exception as e:
        print(f'[ERR] recent records: {e}')
        return []

def fetch_playlists(uid):
    """歌单列表 — 多个备用端点"""
    urls = [
        f'https://music.163.com/api/user/playlist?uid={uid}&limit=1000&offset=0',
        f'https://music.163.com/api/playlist/user/playlist?uid={uid}&offset=0&limit=200',
    ]
    for url in urls:
        try:
            data = fetch_json(url)
            playlists = data.get('playlist', []) or []
            # -462 = 反爬验证, 跳过
            if data.get('code') == -462:
                continue
            results = []
            for p in playlists:
                if not p:
                    continue
                pid = p.get('id', '')
                is_self = str(p.get('userId', '')) == str(uid)
                ct = p.get('createTime', 0)
                ct_str = ''
                if ct and ct > 0:
                    try:
                        ct_str = time.strftime('%Y-%m-%d', time.localtime(ct / 1000))
                    except:
                        ct_str = str(ct)
                results.append({
                    'id': pid,
                    'name': p.get('name', ''),
                    'trackCount': p.get('trackCount', 0),
                    'type': 'self' if is_self else 'subscribed',
                    'createTime': ct_str,
                    'description': (p.get('description') or '')[:300],
                    'tags': p.get('tags', []) or [],
                    'playCount': p.get('playCount', 0),
                })
            if results:
                return results
        except Exception as e:
            print(f'[ERR] playlists ({url}): {e}')
            continue
    print('[WARN] 歌单接口触发了反爬验证，暂时跳过歌单分析')
    return []

def _format_track(item):
    if not item or not item.get('song'):
        return None
    s = item['song']
    return {
        'name': s.get('name', ''),
        'artist': '/'.join(a.get('name', '') for a in s.get('ar', []) if a),
        'album': (s.get('al') or {}).get('name', ''),
    }

# ============================================================
# 数据分析引擎
# ============================================================

def analyze(age, life_stage, film_text, all_time, recent, playlists):
    """基于真实数据进行聚合分析，输出结构化 JSON 供前端渲染"""

    # --- 1. 艺术家频次统计 ---
    artist_count = {}
    for t in (all_time or []):
        if t:
            a = t['artist']
            artist_count[a] = artist_count.get(a, 0) + 1
    top_artists = sorted(artist_count.items(), key=lambda x: -x[1])[:10]
    top_artists = [{'name': n, 'count': c} for n, c in top_artists]

    # --- 2. 歌单时间线 ---
    self_pls = [p for p in playlists if p['type'] == 'self' and p['createTime']]
    self_pls.sort(key=lambda p: p['createTime'])
    sub_pls = [p for p in playlists if p['type'] == 'subscribed']
    timeline_events = []
    for p in self_pls:
        timeline_events.append({
            'time': p['createTime'],
            'name': p['name'],
            'tracks': p['trackCount'],
            'type': 'self'
        })

    # --- 3. 艺术家风格推断（基于已知映射）---
    GENRE_MAP = {
        'Imagine Dragons': '摇滚', 'OneRepublic': '流行摇滚', 'Coldplay': '摇滚/流行',
        'RADWIMPS': 'J-Rock', 'Hans Zimmer': '电影配乐', 'Ludwig Göransson': '电影配乐',
        'Charlie Puth': '流行', 'Ed Sheeran': '流行', 'Ariana Grande': '流行',
        'M83': '电子/合成器', 'The Midnight': '电子/合成器', 'FM-84': '电子/合成器',
        'Linkin Park': '摇滚', 'Green Day': '朋克摇滚', 'Fall Out Boy': '摇滚',
        'Taylor Swift': '流行', 'Billie Eilish': '独立流行',
        'Lana Del Rey': '独立流行', 'C418': '氛围/原声',
        '周杰伦': '华语流行', '陈奕迅': '华语流行', '万能青年旅店': '华语摇滚',
        '草东没有派对': '华语摇滚', '告五人': '华语摇滚', '宋冬野': '民谣',
        '赵雷': '民谣', '马頔': '民谣', '坂本龙一': '氛围/极简',
    }
    genre_scores = {}
    for t in (all_time or [])[:50]:
        if t:
            a = t['artist']
            for artist_key, genre in GENRE_MAP.items():
                if artist_key.lower() in a.lower():
                    genre_scores[genre] = genre_scores.get(genre, 0) + 1
                    break
    total_mapped = sum(genre_scores.values()) or 1
    genres = [{'label': g, 'pct': round(v / total_mapped * 100)} for g, v in
              sorted(genre_scores.items(), key=lambda x: -x[1])[:6]]

    # --- 4. 情绪关键词检测 ---
    EMOTION_KW = {
        '失去/离别': ['离别', '离开', '分手', '再见', '告别', '离人', '失去', '想念', '怀念', '好久不见', '怎么舍得'],
        '孤独/自省': ['孤独', '寂寞', '一个人', '独自', '夜里', '失眠', '安静', '沉思'],
        '能量/对抗': ['热血', '燃', '强', '炸', '冲', 'Radioactive', 'Believer', 'Warrior'],
        '治愈/温暖': ['治愈', '温暖', '阳光', '美好', '甜', 'love', 'heart', 'dream'],
        '梦幻/逃离': ['梦', '幻', '天空', '大海', '星空', '宇宙', '飞行', '飘', '浮'],
        '创作/表达': ['BGM', '剪辑', '配乐', '剪片', '创作', '素材', '原声', 'ost'],
    }
    playlist_text = ' '.join(p['name'] + ' ' + p.get('description', '') for p in playlists)
    emotion_hits = {}
    for emo, kws in EMOTION_KW.items():
        score = sum(1 for kw in kws if kw.lower() in playlist_text.lower())
        if score > 0:
            emotion_hits[emo] = score
    emotional_clues = sorted(emotion_hits.items(), key=lambda x: -x[1])[:6]
    emotional_clues = [{'label': e, 'hits': c} for e, c in emotional_clues]

    # --- 5. 自建/收藏比例 ---
    self_count = len(self_pls)
    sub_count = len(sub_pls)
    total_tracks_self = sum(p['trackCount'] for p in self_pls)

    # --- 6. 活跃年份推断 ---
    years = set()
    for p in self_pls:
        if p['createTime'] and len(p['createTime']) >= 4:
            years.add(p['createTime'][:4])
    active_years = sorted(years)

    # --- 7. 电影交叉（若有） ---
    film_count = 0
    if film_text and film_text.strip():
        films = [f.strip() for f in film_text.strip().split('\n') if f.strip()]
        film_count = len(films)

    # --- 8. 生成总结叙事 ---
    top_artist_name = top_artists[0]['name'] if top_artists else '未知'
    artist_variety = len(artist_count)
    summary = _generate_summary(age, life_stage, top_artist_name, artist_variety,
                                 self_count, sub_count, total_tracks_self,
                                 active_years, genres, emotional_clues, film_count,
                                 all_time, recent, self_pls)

    result = {
        'age': age,
        'lifeStage': life_stage,
        'topArtists': top_artists,
        'artistVariety': artist_variety,
        'genres': genres,
        'emotionalClues': emotional_clues,
        'timelineEvents': timeline_events,
        'selfPlaylistCount': self_count,
        'subPlaylistCount': sub_count,
        'totalSelfTracks': total_tracks_self,
        'activeYears': active_years,
        'filmCount': film_count,
        'allTimeCount': len([t for t in (all_time or []) if t]),
        'recentCount': len([t for t in (recent or []) if t]),
        'allTimeTracks': (all_time or [])[:20],       # top 20 给前端展示
        'recentTracks': (recent or [])[:20],
        **summary
    }
    return result


def _generate_summary(age, life_stage, top_artist, variety, self_pls, sub_pls,
                       total_tracks, years, genres, emotions, film_count,
                       all_time, recent, self_pls_list):
    """生成报告各部分的内容"""

    # 评分
    depth = min(10, round(5 + self_pls * 0.1 + variety * 0.03, 1))
    uniqueness = min(10, round(4 + (variety > 20) * 2 + (len(years) > 2) * 2 + (film_count > 20) * 1.5, 1))
    diversity = min(10, round(4 + len(genres) * 0.8 + (film_count > 10) * 1, 1))

    # 模式
    patterns = build_emotion_patterns(emotions, all_time, self_pls_list)
    strengths = build_strengths(self_pls, sub_pls, variety, years, film_count)
    weaknesses = build_weaknesses(self_pls, emotions, age)
    music_recs = build_music_recommendations(genres, top_artist, emotions)
    film_recs = build_film_recommendations(genres, emotions, film_count)
    guidance = build_guidance(age, life_stage, top_artist, self_pls, years)

    return {
        'scores': {'depth': depth, 'uniqueness': uniqueness, 'diversity': diversity},
        'emotionalPatterns': patterns,
        'strengths': strengths,
        'weaknesses': weaknesses,
        'musicRecs': music_recs,
        'filmRecs': film_recs,
        'guidance': guidance,
    }


def build_emotion_patterns(emotions, all_time, self_pls_list):
    patterns = []
    emo_labels = [e['label'] for e in emotions[:4]]
    emo_text_map = {
        '失去/离别': '你的歌单和听歌记录中存在关于离别和怀念的主题。这在青春期的音乐消费中很常见——音乐是处理情感断裂的安全空间。你通过它来理解失去，也通过它来保持连接。',
        '孤独/自省': '独处和自省在你的歌单中留下了清晰的印记。这未必是负面的——许多创造性工作都始于独处时与自己的对话。',
        '能量/对抗': '高能量的摇滚和电子乐在你的歌单中占有重要位置。这是释放压力和建立身份认同的自然方式，尤其在青少年阶段。',
        '治愈/温暖': '你在主动寻找能带来平静和安慰的声音。这表明即使面对复杂的情绪，你也保持着对「好起来」的信念。',
        '梦幻/逃离': '你倾向于通过音乐构建内心的避难所。这给了你处理现实问题的缓冲空间。',
        '创作/表达': '你不仅消费音乐，还按功能分类归档——这是创作型人格的早期信号。你在为尚未完成的作品储备情感材料。',
    }
    for i, label in enumerate(emo_labels[:4]):
        if label in emo_text_map:
            patterns.append({
                'label': label,
                'body': emo_text_map[label],
                'confidence': round(0.7 + 0.05 * (len(emotions) - i), 2)
            })

    if not patterns:
        patterns.append({'label': '音乐初探期', 'body': '你的听歌数据还比较新，尚未形成足够密集的模式。这是一个探索的阶段——保持好奇心。', 'confidence': 0.6})

    return patterns


def build_strengths(self_pls, sub_pls, variety, years, film_count):
    strengths = []
    if self_pls >= 5:
        strengths.append({'title': '主动建构审美体系', 'body': f'你创建了 {self_pls} 个自己的歌单，而不是只收藏别人的。这说明你的音乐品味不依赖算法喂养——你在主动定义自己喜欢什么。'})
    if variety >= 30:
        strengths.append({'title': '广泛的音乐好奇心', 'body': f'你的听歌记录覆盖了 {variety}+ 位不同艺术家——超出了大部分同龄人的范围。这意味着你的审美框架更灵活、更有弹性。'})
    if len(years) >= 3:
        strengths.append({'title': '持续的自我记录', 'body': f'从 {years[0]} 到 {years[-1]}，{len(years)} 年间你持续在整理自己的音乐世界。这是一种罕见的自我叙事习惯。'})
    if film_count >= 30:
        strengths.append({'title': '跨媒介的审美统一', 'body': f'你的 {film_count} 部观影记录和音乐品味之间存在清晰的主题呼应——你的审美不是随机的，而是一个统一的坐标系。'})
    if not strengths:
        strengths.append({'title': '探索精神', 'body': '你正在积极地接触和整理音乐——这是建立自我认知的第一步，也是最重要的一步。'})
    return strengths


def build_weaknesses(self_pls, emotions, age):
    weaknesses = []
    emo_labels = [e['label'] for e in emotions]
    if '失去/离别' in emo_labels:
        weaknesses.append({'title': '注意审美化的悲伤', 'body': '你的歌单中有明显的离别和怀念主题。音乐是处理情感的好工具，但也要留意：过度消费悲伤内容可能形成回音壁效应——你越听，它越像你的全部。'})
    if self_pls > 20 and '创作/表达' not in emo_labels:
        weaknesses.append({'title': '输入与输出的落差', 'body': f'你建立了 {self_pls} 个歌单，积累了庞大的素材库。工具箱已经完备——下一步需要考虑的是：你准备用这些材料建造什么？'})
    if age <= 18 and '能量/对抗' in emo_labels:
        weaknesses.append({'title': '摇滚能量之后是什么', 'body': '高能量音乐是很好的宣泄出口，但它不能替代对情绪的深度处理。在摇滚停下来的时候，你还需要学会安静地与自己的情绪呆在一起。'})
    if not weaknesses:
        weaknesses.append({'title': '可以更主动地探索', 'body': '目前的音乐消费偏向主流。尝试跳出舒适区——那些第一听觉得「奇怪」的音乐，往往是品味进化的入口。'})
    return weaknesses[:3]


def build_music_recommendations(genres, top_artist, emotions):
    genre_labels = [g['label'] for g in genres]
    recs = []

    # 基于已有风格推荐互补声音
    if '摇滚' in str(genre_labels) and 'J-Rock' not in str(genre_labels):
        recs.append({'name': 'toe — For Long Tomorrow', 'reason': '日本数学摇滚乐队。像电影配乐一样精密复杂，但每一拍都是用人类的手打出来的。诺兰配乐的同等智力密度，但完全不依赖管弦乐队。'})

    if '电影配乐' in str(genre_labels):
        recs.append({'name': '坂本龙一 — Async', 'reason': '你已经在听电影配乐——坂本是下一步。不急不躁的深邃，不痛的平静。他晚年患癌后做的音乐有一种你目前的谱系中缺少的频率：将死的平静。'})

    has_healing = any(e['label'] in ['治愈/温暖', '梦幻/逃离'] for e in emotions)
    if has_healing:
        recs.append({'name': 'Nujabes — Modal Soul', 'reason': '日本制作人，将爵士采样和嘻哈节拍融为温暖的器乐。与 lofi 完全吻合但更高一个维度——有哲学感但不煽情。是「剪片用」最想要但还没找到的那种声音。'})

    recs.append({'name': 'Bon Iver — For Emma, Forever Ago', 'reason': '一个人在森林小屋里度过冬天，录了一张改变独立音乐史的专辑。比摇滚安静，比中文民谣更抽象。如果你有关于失去的歌单——这张专辑是那个主题的英语最高版本。'})

    if '流行' in str(genre_labels):
        recs.append({'name': 'Frank Ocean — Blonde', 'reason': '如果你听 Ariana Grande 和流行 R&B——Frank Ocean 是同一个情绪空间的更高维度。歌词是碎片化的意象，让你自己拼故事。无数人在分手后反复播放。'})

    return recs[:4]


def build_film_recommendations(genres, emotions, film_count):
    recs = []
    recs.append({'name': '《暖暖内含光》', 'reason': '如果痛苦可以被科技删除，你删不删？一对分手的情侣选择删除彼此的记忆，但在记忆崩塌中重新爱上了对方。如果「失去/离别」是你的主题——这部电影是它的终极表达。'})
    recs.append({'name': '《帕特森》', 'reason': '一个公交车司机每天写诗，不发表，不出名。笔记本被狗咬碎后有人告诉他：「空白的页面有时候是最美的。」你的观影体系缺少「不需要成为杰作的人生」。'})
    recs.append({'name': '《晒后假日》', 'reason': '不需要任何戏剧性事件就能让你在片尾坐很久。很多年后才发现，有人在那个夏天就在告别——它处理失去的方式是完全反戏剧化的。'})
    return recs


def build_guidance(age, life_stage, top_artist, self_pls, years):
    g = []

    # 年龄锚定的指引
    if age <= 16:
        g.append('你正在人格形成最关键的窗口期。音乐和电影不是「消遣」——它们正在塑造你如何理解自己和世界。选好你的输入。')
        g.append('14-16 岁开始系统整理自己的品味——这比大多数同龄人早了 3-5 年。保持这个习惯，但不要让它变成你与众不同的「证明」。')
    elif age <= 18:
        g.append('从 14 岁走到 18 岁，你的品味已经经历了一次完整的进化。大学即将（或已经）是一个重置按钮：带着你已经建立的坐标系，去遇见那些与坐标系格格不入的声音。')
    else:
        g.append('你已经度过了人格建构最密集的阶段。接下来不是「发现更多」，而是「用已经有的去建造」。')

    g.append(f'你的歌单里有 {self_pls} 个你自己建的——它们不是播放列表，是你人格自传的章节标题。但现在该写正文了：用你收集的声音去做点什么。')
    g.append('允许自己不酷。品味不是人格——你也需要烂俗的喜剧、没有深度的流行歌、说不清为什么就是开心的瞬间。')

    if len(years) >= 3:
        g.append(f'从 {years[0]} 到 {years[-1]}——回头看，你会发现每一个阶段的音乐选择都对应着那个阶段的你。这份「自我历史档案」的价值，你会随着年龄增长越来越清楚地感受到。')

    g.append('同龄人可能读不懂你的品味——这不是他们的错，也不是你的。找到能读懂你的人需要时间。在那之前，别把孤独浪漫化。孤独只是暂时的。')

    if age <= 18:
        g.append('大学是找到同类的机会。社团、选修课、BBS——总有一个地方聚集着和你听同样音乐的人。别一个人待着。')
        g.append('从消费者到创造者的转折点通常在 18-22 岁。你已经准备好了工具箱。接下来四年——开机吧。')
    else:
        g.append('完美的工具箱不会自动产出作品。避免用「准备」替代「开始」——30% 的完成度发出去，比 100% 的「还在优化」有价值一万倍。')

    g.append('你的品味不会「完成」——它会一直进化。十年前你喜欢的东西可能会让你脸红，这是好事。品味应该让你尴尬。')
    g.append('最后——听音乐是开心的。看电影是开心的。别让分析报告把它变成另一件需要「做好」的事。')

    return g[:9]


# ============================================================
# HTTP 服务器
# ============================================================

class EchoHandler(BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, content_type):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'404 Not Found')

    def do_OPTIONS(self):
        self._send_json({})

    def do_GET(self):
        path = self.path.split('?')[0]
        cwd = os.path.dirname(os.path.abspath(__file__))

        if path == '/' or path == '/index.html':
            self._serve_file(os.path.join(cwd, 'index.html'), 'text/html; charset=utf-8')
        elif path == '/echo-tutorial.html':
            self._serve_file(os.path.join(cwd, 'echo-tutorial.html'), 'text/html; charset=utf-8')
        elif path == '/echo-spec.html':
            self._serve_file(os.path.join(cwd, 'echo-spec.html'), 'text/html; charset=utf-8')
        else:
            self._send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        if self.path != '/api/analyze':
            self._send_json({'error': 'Not found'}, 404)
            return

        # 读取请求体
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            req = json.loads(body.decode('utf-8'))
        except:
            self._send_json({'error': 'Invalid JSON'}, 400)
            return

        uid = req.get('uid', '').strip()
        age = req.get('age', 18)
        life_stage = req.get('lifeStage', '')
        film_text = req.get('filmList', '')
        has_screenshot = bool(req.get('hasScreenshot'))

        # 验证 UID
        if not uid or not uid.isdigit():
            self._send_json({
                'error': 'missing_uid',
                'message': '请填写网易云用户 ID（在个人主页右上角可以找到，是一串纯数字）',
                'demo_uid': '7847515553'
            }, 400)
            return

        print(f'[REQ] uid={uid} age={age} stage={life_stage} film_count={len(film_text.split(chr(10))) if film_text else 0}')

        # Step 1: 拉取真实数据
        all_time = fetch_all_time_records(uid)
        recent = fetch_recent_records(uid)
        playlists = fetch_playlists(uid)

        all_time_count = len(all_time)
        recent_count = len(recent)
        playlist_count = len(playlists)

        print(f'[DATA] allTime={all_time_count} recent={recent_count} playlists={playlist_count}')

        if all_time_count == 0 and recent_count == 0:
            self._send_json({
                'error': 'no_data',
                'message': '未能获取到听歌数据。请检查 UID 是否正确，或者该用户的听歌排行未公开。'
            }, 400)
            return

        # Step 2: 分析
        result = analyze(age, life_stage, film_text, all_time, recent, playlists)

        # 附加元信息
        result['nickname'] = req.get('nickname', f'网易云用户 {uid}')
        result['uid'] = uid
        result['hasScreenshot'] = has_screenshot
        result['allTimeCount'] = all_time_count
        result['recentCount'] = recent_count
        result['playlistCount'] = playlist_count
        result['success'] = True
        if playlist_count == 0:
            result['playlistNotice'] = '歌单接口暂时受限（反爬机制），当前分析基于听歌排行数据'

        print(f'[DONE] uid={uid} top={result["topArtists"][0]["name"] if result["topArtists"] else "N/A"} pl={playlist_count}')
        self._send_json(result)


def main():
    print(f'🎵 回响 Echo 服务器启动...')
    print(f'   地址: http://localhost:{PORT}')
    print(f'   API:  http://localhost:{PORT}/api/analyze')
    print(f'   按 Ctrl+C 停止')
    server = HTTPServer(('0.0.0.0', PORT), EchoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n👋 服务器已停止')
        server.shutdown()


if __name__ == '__main__':
    main()