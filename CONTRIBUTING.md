# 贡献指南

感谢你对 Quanti 的关注！欢迎通过以下方式参与贡献。

## 报告问题

- 使用 GitHub Issues 提交 bug 报告或功能建议
- 描述问题时请包含：Python 版本、操作系统、复现步骤、错误日志

## 提交代码

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feat/my-feature`
3. 编写代码和测试
4. 确保所有测试通过：`pytest tests/ -v`
5. 提交 Pull Request

## 开发环境

```bash
git clone https://github.com/your-username/quanti.git
cd quanti
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 代码规范

- 使用 [Ruff](https://github.com/astral-sh/ruff) 进行代码检查
- 行宽限制 100 字符
- 目标 Python 版本 3.11+
- 所有新功能需附带测试

```bash
# 代码检查
ruff check quanti/ tests/

# 自动修复
ruff check --fix quanti/ tests/
```

## 项目结构

- `quanti/` - 核心包代码
- `tests/` - 测试文件，与 `quanti/` 模块对应
- `strategies/` - 策略文件，每个文件一个策略类
- `web/` - Vue 3 前端项目

## 添加新策略

在 `strategies/` 目录下创建 `.py` 文件，继承 `BaseStrategy` 即可被自动加载。

## 添加新因子

在 `quanti/factors/technical.py` 中添加计算函数，或使用 `@register_factor` 装饰器注册到全局因子表。
