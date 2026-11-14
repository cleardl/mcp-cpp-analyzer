# MCP C++ Analyzer

面向 AI Agent 的 C/C++ 语义查询 MCP 服务。通过 clangd 和 compile_commands.json，提供批量符号搜索、定义与引用、调用关系、类型层级和诊断查询。

## 使用

需要 Python 3.13+、uv、clangd，以及目标 C/C++ 项目的 compile_commands.json。

在本仓库目录安装依赖：

~~~powershell
uv sync --locked --no-build
~~~

只需在 MCP 客户端中配置 stdio 服务，使用本仓库已安装环境中的 FastMCP。以下是常见 mcp.json 格式，按客户端要求放入对应配置文件并替换路径：

~~~json
{
  "mcpServers": {
    "cpp-analyzer": {
      "command": "/path/to/mcp-cpp-analyzer/.venv/bin/fastmcp",
      "args": ["run", "/path/to/mcp-cpp-analyzer/server.py:create_mcp", "--transport", "stdio", "--log-level", "WARNING", "--no-banner"],
      "cwd": "/path/to/cpp-project",
      "env": {
        "CPP_ANALYZER_COMPILE_DB_PATH": "/path/to/build",
        "CPP_ANALYZER_CLANGD_PATH": "/path/to/clangd"
      }
    }
  }
}
~~~

Windows 的可执行文件位于 .venv/Scripts/fastmcp.exe。cwd 为待分析项目根目录；CPP_ANALYZER_COMPILE_DB_PATH 为包含 compile_commands.json 的目录；CPP_ANALYZER_CLANGD_PATH 为 clangd 可执行文件。相对环境变量路径以 cwd 为基准，缺失或无效时启动报错。

源码与协议文本固定使用 UTF-8，源码解码失败直接报错，不提供编码配置。--no-banner 同时跳过 FastMCP 启动横幅中的更新检查。

客户端负责启动和关闭 MCP 子进程，不需要提前启动服务。每个独立的 stdio MCP 进程各自启动一份 clangd；同一应用是否复用连接由客户端决定。若客户端支持启动超时配置，建议设为 90 秒；工具超时建议设为 150 秒。

默认启用 Code Mode，客户端暴露 search、get_schema、execute 三个工具。先发现接口，再通过 execute 中的 call_tool 批量查询、筛选结果。首次连接通过 execute 调用 workspace_status 核对目标项目。

## 查询接口

以下接口通过 execute 内的 call_tool 调用：

| 工具 | 用途 |
| --- | --- |
| search_symbols | 一次搜索多个名称，按路径、SymbolKind 过滤，返回 symbol_id |
| query_symbols | targets × operations 批量语义查询，最多 64 组 |
| query_files | 多文件符号大纲与当前文档版本的源码诊断 |
| read_results | 批量翻页或使用 result_id 按需展开完整结果 |
| workspace_status | clangd 版本、能力、进程失败和索引活动，不依赖后端健康检查通过 |

query_symbols 支持 definition、declaration、implementation、type_definition、hover、references、callers、callees、supertypes、subtypes、context、symbol_info。

execute 示例：

~~~python
result = await call_tool("query_symbols", {
    "targets": [{"file": "src/client.cpp", "line": 42, "column": 8}],
    "operations": ["definition", "callers", "callees"],
    "fields": ["file", "line", "column", "name", "symbol_id"],
    "limit": 10,
})
return result
~~~

targets 也可使用 search_symbols 返回的 symbol_id。行列从 1 开始，列按 Unicode 字符计数，制表符计为一个字符。完整结果的 range/call_sites 为 [起始行,起始列,结束行,结束列]，不包含结束位置。

## 结果与分页

- 默认 compact 使用 files 表和 columns/rows，减少重复路径；fields 只返回所需列。
- detail="standard" 返回具名对象；detail="full" 包含完整范围、签名和源码片段。
- query_symbols 每批最多 64 个 target/operation 组合，默认最多输出 80 项、24000 字符。
- truncated 表示结果被截断，用 cursor 继续翻页；result_id 可从头重新展开。二者均传给 read_results 的 cursors 参数。
- 文件变化会使旧 symbol_id 和结果失效，需要重新发现符号；过期或服务重启后也需要重新查询。
- 部分失败逐项返回错误，全部失败使用 MCP isError=true；空结果与执行失败不同。跨文件查询依赖 clangd 索引，空结果不能证明全仓不存在。

## 可选配置

在 MCP 客户端配置的 env 中设置：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| CPP_ANALYZER_TOOL_MODE | code | code 在沙箱内组合查询并筛选返回值；direct 直接暴露批量工具；search 按需发现工具 |

code 使用 FastMCP 的实验性 [Code Mode](https://gofastmcp.com/docs/servers/transforms/code-mode)，每次最多 50 次工具调用、120 秒。search 使用 [Tool Search](https://gofastmcp.com/docs/servers/transforms/tool-search)。
