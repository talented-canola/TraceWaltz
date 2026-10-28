修复 api.py 的 paginate。page 从 1 开始，size 表示每页条数，返回对应切片，不修改输入列表。
空列表或超出最后一页时返回空列表。page、size 必须是正整数；bool 和其他类型抛出 TypeError，
非正整数抛出 ValueError。保持测试文件不变。

验证命令：python -m unittest discover -s tests -v
