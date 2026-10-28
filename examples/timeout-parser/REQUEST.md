修复 config.py 的 parse_timeout。None 使用 default，非负整数原样返回（0 也是有效值）。
字符串去掉两端空白后只能是十进制数字，转换为整数；其他字符串或负整数抛出 ValueError。
bool、float、容器等其他类型抛出 TypeError。保持测试文件不变。

验证命令：python -m unittest discover -s tests -v
