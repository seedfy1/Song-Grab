KaiTi-SC-Regular.ttf —— 楷体系中文字体（App 内嵌）

来源：霞鹜文楷 LXGW WenKai v1.510
      https://github.com/lxgw/LxgwWenKai
许可：SIL Open Font License 1.1，全文见同目录 OFL.txt

本文件是「修改版」（OFL 术语）：
1. 子集化：只保留基本拉丁/拉丁扩展、常用符号与标点、CJK 标点、
   平假名/片假名、CJK 统一表意文字基本区（U+4E00-9FFF）、全角字符；
2. 改了 name 表（family name 改为 KaiTi SC），以避开 OFL 保留字体名。
未包含：CJK 扩展 A/B 区生僻字、Emoji —— 界面里不要用 emoji（会显示方块）。

重新生成的方法：
  pyftsubset LXGWWenKai-Regular.ttf \
    --unicodes="U+0020-007E,U+00A0-017F,U+2000-206F,U+2190-21FF,U+2460-24FF,U+25A0-25FF,U+3000-303F,U+3040-30FF,U+4E00-9FFF,U+FF00-FFEF" \
    --layout-features='*' --no-hinting --desubroutinize --output-file=sub.ttf
  再用 fontTools 把 name 表 nameID 1/2/3/4/6/16/17 改成 KaiTi SC 系列名字。
