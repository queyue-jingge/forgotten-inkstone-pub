#!/usr/bin/env python3
"""讲课台 · 分身侧小工具。

分身讲某页之前先跑这个，就能拿到"这页长什么样（图）+ 写了什么（文本层）"。

用法：
  python3 teach.py view [course] [page]     # 不传 page 就用网页当前页
      → 打印该页信息；末行给出一张 jpg 的绝对路径，分身用 Read 打开看图。
  python3 teach.py where                     # 只看当前网页停在哪页

course: semi(半导体物理) | signals(信号与系统)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S  # noqa: E402


def resolve(course, page):
    st = S.load_state(course)
    if not st:
        print(f"未知课程：{course}（可选：{'/'.join(S.COURSES)}）")
        sys.exit(1)
    vol = st["volume"]
    v = S.VOLUMES[vol]
    if page is None:
        page = st["page"]
    page = int(page)
    if page < 1 or page > v["pages"]:
        print(f"页码超出：卷 {vol} 只有 1..{v['pages']}")
        sys.exit(1)
    return st, vol, v, page


def cmd_view(course, page):
    st, vol, v, page = resolve(course, page)
    img, err = S.render_page(vol, page)
    if err:
        print("渲图失败：" + err)
        sys.exit(1)
    print(f"课程: {S.COURSES[course]['title']}")
    print(f"卷  : {v['title']}")
    print(f"PDF页: {page}   书页: {page - v['offset']}   共 {v['pages']} 页")
    txt = S.page_text(vol, page)
    if txt:
        print("--- 该页文字层（图注/公式仍须看图）---")
        print(txt)
    else:
        print("--- 此卷无文字层，纯看图 ---")
    print("图  : " + img)


def cmd_where(course):
    st, vol, v, page = resolve(course, None)
    print(f"{S.COURSES[course]['title']} · {v['title']}")
    print(f"当前停在 PDF 第 {page} 页（书页 {page - v['offset']}）")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = args[0]
    course = args[1] if len(args) > 1 else "semi"
    if cmd == "view":
        page = args[2] if len(args) > 2 else None
        cmd_view(course, page)
    elif cmd == "where":
        cmd_where(course)
    else:
        print("未知命令：" + cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
