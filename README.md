# MCP C++ Analyzer

面向 AI Agent 的 C/C++ 语义查询服务。通过 clangd 和 compile_commands.json，提供批量符号搜索、定义与引用、调用关系、类型层级和当前文档诊断。

## 二进制部署

Windows 发布程序使用 stdio，默认启用 Code Mode。Python、FastMCP 和 Code Mode 运行依赖已包含在程序中，使用端无需安装 Python 或保留源码。

将 mcp-cpp-analyzer.exe 与 clangd.exe 放在同一个 bin 目录，保留与 bin 相邻的 lib/clang 资源目录。客户端配置示例：

~~~json
{
  "mcpServers": {
    "cpp-analyzer": {
      "command": "C:/tools/cpp-analyzer/bin/mcp-cpp-analyzer.exe",
      "cwd": "C:/work/cpp-project",
      "env": {
        "CPP_ANALYZER_COMPILE_DB_PATH": "C:/work/build"
      }
    }
  }
}
~~~

编译数据库目录须包含 compile_commands.json；相对路径以 cwd 为基准。源码和协议文本固定使用 UTF-8，解码失败直接报错。服务不检查更新，不需要额外后端配置文件或启动参数。

客户端负责进程生命周期，每个独立服务进程复用一份 clangd。启动超时建议 90 秒，工具超时建议 150 秒。首次连接通过 execute 调用 workspace_status 核对目标工作区与数据库。

## 查询接口

客户端暴露 search、get_schema、execute。先发现接口，再在 execute 中通过 await call_tool(name, arguments) 批量查询、筛选并 return：

| 内部工具 | 用途 |
| --- | --- |
| search_symbols | 批量名称发现、路径和 SymbolKind 过滤，返回 symbol_id |
| query_symbols | targets × operations 批量语义查询，最多 64 组 |
| query_files | 多文件符号大纲与当前文档版本诊断 |
| read_results | 批量分页或按 result_id 展开缓存结果 |
| workspace_status | clangd 能力、进程失败、工作区和索引活动 |

query_symbols 支持 definition、declaration、implementation、type_definition、hover、references、callers、callees、supertypes、subtypes、context、symbol_info。

~~~python
result = await call_tool("query_symbols", {
    "targets": [{"file": "src/client.cpp", "line": 42, "column": 8}],
    "operations": ["definition", "callers", "callees"],
    "fields": ["file", "line", "column", "name", "symbol_id"],
    "limit": 10,
})
return result
~~~

优先复用符号发现返回的 symbol_id。位置从 1 开始，列按 Unicode codepoint 计数，制表符计为一个字符。

- compact 使用 files 与 columns/rows；fields 投影所需列，保留文件映射与分页、错误信息。
- standard 返回具名对象，full 按需包含详细范围和源码。默认输出预算为 80 项、24000 字符。
- truncated 时用 cursor 继续；result_id 可从头展开，二者传给 read_results 的 cursors。
- 文件变化、句柄过期或进程重启后重新发现。部分失败逐项返回，全部失败使用 MCP isError=true。
- 跨文件结果依赖 clangd 索引，空结果不能证明全仓不存在，诊断不等同工程编译。
- index_activity 区分未观察到索引、索引中和空闲；空闲不代表全工程覆盖，index_complete 保持未知。空的索引查询返回 INDEX_COVERAGE_UNVERIFIED。
- definition 的 location_kind 区分 AST 定义、声明和索引位置；仅有声明时返回 IMPLEMENTATION_UNCONFIRMED。
- 调用结果保留调用点。能够用调用点 AST 确认定义时优先使用该位置；与索引冲突时同时保留 index_location 和警告，fields 也不能隐藏冲突证据。linkage_verified=false 表示未验证最终链接选择。

Code Mode 每次最多 50 次内部工具调用、120 秒。CPP_ANALYZER_TOOL_MODE 默认为 code；direct 或 search 需显式设置。

外层工具只返回一份 structuredContent，Code Mode 内部使用结构化对象串联查询；不再附加相同 JSON 的文本副本。工具错误仍保留 isError 和错误内容。fields 的允许值同时提供在完整 JSON schema 和简略参数说明中。

## 开发者构建

构建需要 Python 3.13+ 和 uv，使用 PyInstaller 收集解释器与依赖，不编译为 C。打包工具通过 build 依赖组锁定，与使用端无关：

~~~powershell
uv sync --locked --group build
.venv/Scripts/python.exe -X utf8 -B build.py --output dist
~~~

构建前须提交源码并保持工作区干净；程序嵌入版本和 Git revision，通过 workspace_status.build 可核对来源。输出 exe 和第三方许可清单；clangd 及资源目录单独随包发布。

入口在导入运行框架前保留独立协议管道，将 Python print 与原生 stdout 日志导向 stderr，再向 MCP 传入显式协议流；内置 UTF-8、stdio 和关闭启动横幅。Code Mode 的 Monty worker 一并封装在 exe 中，运行时固定使用随包程序。发布前验证 Code Mode、真实语义查询、随包 clangd 资源和正常退出清理。

回归检查使用 stdlib unittest，只启动 clangd 解析测试文件，不调用 C/C++ 编译器。设置 CPP_ANALYZER_TEST_CLANGD 指向 clangd 后运行：

~~~powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
~~~
