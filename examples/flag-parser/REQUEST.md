修复 settings.py 的 parse_flag。保留 bool 原样；字符串去除空格并忽略大小写，
true/yes/on/1 解析为 True，false/no/off/0 解析为 False。
未知字符串抛出 ValueError，其他类型抛出 TypeError。保持已有测试文件不变。

验证命令：python -m unittest discover -s tests -v
