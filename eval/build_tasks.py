"""评测集生成器 + 自检器。

## 为什么评测集要用代码生成而不是手写 JSON

手写 JSONL 有两个致命问题：
1. **答案会写错。** 60 条里手滑写错三个行号，你的实验数据就带着 5% 的噪声，
   而且你根本不会发现 —— 因为「agent 没答对」和「标准答案写错了」
   在结果上长得一模一样。
2. **改不动。** 换了语料仓库（比如换成 flask），60 条要重抄一遍。

这里把评测集定义成 Python 数据结构，生成时**自动校验每一条断言**：

    符号确实在仓库里存在吗？
    符号确实在它该在的那个文件里吗？
    断言里的文件名写对了吗？

校验不过直接报错退出，不产出 JSONL。
**评测集自带自检，这本身就是这个项目值得讲的一点。**

用法：
    python -m eval.build_tasks            # 生成 eval/tasks.jsonl
    python -m eval.build_tasks --check    # 只校验不生成
"""

import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "sandbox", "requests_src"))
OUT = os.path.join(os.path.dirname(__file__), "tasks.jsonl")

# ============================================================================
# A 类：精确查找（20 条）
#
# 特征：问的是**精确符号**在哪。措辞刻意口语化，测的是
#       「从术语/功能描述定位到具体文件和函数」的能力。
#       这类问题向量检索最容易翻车 —— 因为专有名词在语义空间里没有邻居。
# ============================================================================

A = [
    dict(id="A01", type="A",
         question="重定向到别的域名时，requests 会判断要不要把 Authorization 请求头去掉。判断逻辑写在哪个文件的哪个函数？",
         checks={"must_contain": ["should_strip_auth", "sessions.py"],
                 "must_not_contain": ["models.py", "adapters.py"]},
         reference="requests/sessions.py:154 should_strip_auth()",
         why="精确符号定位。函数名是生造的组合词，语义检索很难命中。"),

    dict(id="A02", type="A",
         question="客户端证书校验（verify 参数最终真正生效的地方）是在哪个文件的哪个函数里做的？",
         checks={"must_contain": ["cert_verify", "adapters.py"]},
         reference="requests/adapters.py:307 HTTPAdapter.cert_verify()",
         why="verify 参数跨越多层，要找到最终落地点。"),

    dict(id="A03", type="A",
         question="urllib3 返回的底层响应是在哪儿被转换成 requests 的 Response 对象的？",
         checks={"must_contain": ["build_response", "adapters.py"]},
         reference="requests/adapters.py:365 HTTPAdapter.build_response()",
         why="两个库边界上的转换函数，需要理解调用链才知道找谁。"),

    dict(id="A04", type="A",
         question="requests 的 HTTP 连接池是在哪个函数里初始化的？",
         checks={"must_contain": ["init_poolmanager", "adapters.py"]},
         reference="requests/adapters.py:239 HTTPAdapter.init_poolmanager()",
         why="连接池创建的唯一切入点。"),

    dict(id="A05", type="A",
         question="跟着一串 301/302 一路跳到底的逻辑，是哪个函数实现的？",
         checks={"must_contain": ["resolve_redirects", "sessions.py"]},
         reference="requests/sessions.py:186 SessionRedirectMixin.resolve_redirects()",
         why="重定向循环处理，是个生成器函数。"),

    dict(id="A06", type="A",
         question="重定向之后要重新设置 Authorization 头，这个动作在哪个函数里？",
         checks={"must_contain": ["rebuild_auth", "sessions.py"]},
         reference="requests/sessions.py:309 rebuild_auth()",
         why="和 A01 是同一个机制的两半，容易混淆。"),

    dict(id="A07", type="A",
         question="重定向之后代理配置要重新计算，这部分代码在哪个函数？",
         checks={"must_contain": ["rebuild_proxies", "sessions.py"]},
         reference="requests/sessions.py:334 rebuild_proxies()",
         why="同上，重定向三件套之一。"),

    dict(id="A08", type="A",
         question="requests 根据 URL 前缀挑选适配器的逻辑在哪个函数？（就是 http:// 和 https:// 各找一个适配器那套）",
         checks={"must_contain": ["get_adapter", "sessions.py"]},
         reference="requests/sessions.py:870 Session.get_adapter()",
         why="mount 机制的查询侧。"),

    dict(id="A09", type="A",
         question="会话级配置和单次请求配置冲突时，合并规则写在哪个函数里？",
         checks={"must_contain": ["merge_setting", "sessions.py"]},
         reference="requests/sessions.py:76 merge_setting()",
         why="配置优先级的核心实现。"),

    dict(id="A10", type="A",
         question="params 参数最终被拼到 URL 上，是哪个函数干的？",
         checks={"must_contain": ["prepare_url", "models.py"]},
         reference="requests/models.py:483 PreparedRequest.prepare_url()",
         why="URL 组装，含 IDNA 编码。"),

    dict(id="A11", type="A",
         question="data / json / files 这几个参数变成最终请求体的转换代码在哪个函数？",
         checks={"must_contain": ["prepare_body", "models.py"]},
         reference="requests/models.py:576 PreparedRequest.prepare_body()",
         why="请求体组装，分支很多。"),

    dict(id="A12", type="A",
         question="从响应里把 Location 头取出来算出下一个跳转地址，是哪个函数？",
         checks={"must_contain": ["get_redirect_target", "sessions.py"]},
         reference="requests/sessions.py:134 SessionRedirectMixin.get_redirect_target()",
         why="重定向第一步，函数名里没有任何「Location」线索。"),

    dict(id="A13", type="A",
         question="raise_for_status() 这个方法的实现在哪？",
         checks={"must_contain": ["raise_for_status", "models.py"]},
         reference="requests/models.py:1144 Response.raise_for_status()",
         why="最常用的 API 之一，考察能否定位到具体文件。"),

    dict(id="A14", type="A",
         question="流式读取响应内容（一块一块读）的那个方法叫什么，在哪个文件？",
         checks={"must_contain": ["iter_content", "models.py"]},
         reference="requests/models.py:907 Response.iter_content()",
         why="stream=True 的核心方法。"),

    dict(id="A15", type="A",
         question="有个属性会根据响应内容去「猜」编码，它叫什么，在哪实现的？",
         checks={"must_contain": ["apparent_encoding", "models.py"]},
         reference="requests/models.py:897 Response.apparent_encoding",
         why="属性而非方法，且依赖 chardet/charset_normalizer。"),

    dict(id="A16", type="A",
         question="hook 的注册和分发机制在哪个文件？分发函数叫什么？",
         checks={"must_contain": ["dispatch_hook", "hooks.py"]},
         reference="requests/hooks.py:32 dispatch_hook()",
         why="hooks.py 只有 48 行，但要确认分发函数而非默认钩子。"),

    dict(id="A17", type="A",
         question="默认 hook 的初始化函数叫什么？在哪个文件？",
         checks={"must_contain": ["default_hooks", "hooks.py"]},
         reference="requests/hooks.py:25 default_hooks()",
         why="和 A16 同文件，考察区分度。"),

    dict(id="A18", type="A",
         question="HTTP 头是大小写不敏感的，requests 里实现这个语义的数据结构叫什么？在哪个文件？",
         checks={"must_contain": ["CaseInsensitiveDict", "structures.py"]},
         reference="requests/structures.py:20 CaseInsensitiveDict",
         why="核心数据结构，名字长且是复合词。"),

    dict(id="A19", type="A",
         question="从字典创建一个 cookie jar，用哪个函数？在哪个文件？",
         checks={"must_contain": ["cookiejar_from_dict", "cookies.py"]},
         reference="requests/cookies.py:564 cookiejar_from_dict()",
         why="cookies.py 有 625 行、56 个符号，考察在大文件里定位。"),

    dict(id="A20", type="A",
         question="HTTP Basic 认证那个 Authorization 头的值，是哪个函数拼出来的？",
         checks={"must_contain": ["_basic_auth_str", "auth.py"]},
         reference="requests/auth.py:34 _basic_auth_str()",
         why="私有函数，且「拼 Authorization 值」这个描述不直接对应函数名。"),
]

# ============================================================================
# B 类：语义理解（20 条）
#
# 特征：问流程/机制，题目里不出现任何符号名。
#       必须读懂多个函数并串起来才能回答 —— 这是向量检索的主场，
#       也是 agentic retrieval 相对吃力的地方。
# ============================================================================

B = [
    dict(id="B01", type="B",
         question="调用 requests.get(url) 之后，从入口一直到拿到 Response 对象，中间依次经过了哪些函数？请按顺序列出关键环节。",
         checks={"must_contain": ["api.py", "sessions.py", "adapters.py"],
                 "must_contain_any": [["Session.request", "session.request"],
                                      ["Session.send", "session.send"],
                                      ["HTTPAdapter.send", "adapter.send"]]},
         reference="api.get -> api.request -> Session.request -> Session.send "
                   "-> HTTPAdapter.send -> build_response",
         why="最经典的调用链，需要跨三个文件。"),

    dict(id="B02", type="B",
         question="timeout 参数是怎么从最外层的 requests.get 一层层传到最底层的？中间有没有被拆分或改造？",
         checks={"must_contain": ["sessions.py", "adapters.py"],
                 "must_contain_any": [["timeout"]]},
         reference="api.request(timeout) -> Session.request(self, timeout) "
                   "-> Session.send(..., timeout=timeout) -> HTTPAdapter.send(timeout=timeout)",
         why="参数透传链，需要逐层确认。"),

    dict(id="B03", type="B",
         question="requests 的 HTTP 连接是怎么复用的？连接池存在哪儿，什么时候新建、什么时候复用？",
         checks={"must_contain": ["poolmanager", "adapters.py"],
                 "must_contain_any": [["init_poolmanager"], ["pool_connections"],
                                      ["pool_maxsize"], ["poolmanager", "pool manager"]]},
         reference="HTTPAdapter 持有 PoolManager；init_poolmanager 创建；"
                   "get_connection_with_tls_context 按 host 取连接复用",
         why="需要理解 urllib3 的 PoolManager 与 requests 的包装关系。"),

    dict(id="B04", type="B",
         question="requests 默认最多跟随多少次重定向？这个上限在哪里被强制执行的？超了会抛什么异常？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["max_redirects", "MAX_REDIRECTS"], ["30"]],
                 "regex": [r"too\s*many\s*redirects"]},
         reference="默认 30 次，在 sessions.py 的 resolve_redirects 里计数，"
                   "超过抛 TooManyRedirects（exceptions.py:106）",
         why="常量 + 异常类型，两个事实都要对。"),

    dict(id="B05", type="B",
         question="用同一个 Session 发多次请求，cookie 是怎么在请求之间保持的？",
         checks={"must_contain": ["cookies"],
                 "must_contain_any": [["RequestsCookieJar", "cookiejar"],
                                      ["extract_cookies_to_jar"], ["prepare_cookies"]]},
         reference="Session.cookies 是 RequestsCookieJar；响应回来经 "
                   "extract_cookies_to_jar 写入，下次请求经 prepare_cookies 读出",
         why="需要串起「写入」和「读出」两个方向。"),

    dict(id="B06", type="B",
         question="stream=True 之后响应内容为什么不会立刻全部下载？内部是怎么控制的？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["iter_content"], ["_content_consumed"],
                                      ["_content"]]},
         reference="Response.content 是惰性属性，首次访问才触发 self.content 消费；"
                   "iter_content 用 stream(..., decode_content=True) 分块生成",
         why="惰性求值机制，需要读 content 属性和 iter_content 两处。"),

    dict(id="B07", type="B",
         question="把 Session 用作上下文管理器（with 语句）时，退出时做了什么？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["__exit__"], ["close"]]},
         reference="Session.__exit__ 调用 self.close()，关闭所有适配器、释放连接池",
         why="短小但需要找到 dunder 方法。"),

    dict(id="B08", type="B",
         question="代理配置有好几个来源：显式传 proxies、环境变量、系统配置。它们的优先级是什么？",
         checks={"must_contain": ["utils.py"],
                 "must_contain_any": [["merge_environment_settings"],
                                      ["get_environ_proxies"], ["should_bypass_proxies"],
                                      ["select_proxy"]]},
         reference="Session.merge_environment_settings 决定：trust_env 为假时只用显式传的；"
                   "为真时用 get_environ_proxies 读环境变量，再与显式配置合并，显式优先",
         why="三路来源的合并顺序，是典型的「要读三个函数才能答」的问题。"),

    dict(id="B09", type="B",
         question="requests 发请求时不设 User-Agent，最终发出去的 User-Agent 是从哪儿来的？",
         checks={"must_contain": ["utils.py"],
                 "must_contain_any": [["default_user_agent"], ["default_headers"]]},
         reference="utils.default_headers() 生成默认头，User-Agent 值来自 "
                   "utils.default_user_agent()，形如 python-requests/2.x.x",
         why="需要区分 default_headers 和 default_user_agent 两个函数。"),

    dict(id="B10", type="B",
         question="verify=False 的时候，requests 内部具体关掉了什么？证书验证真的完全没做吗？",
         checks={"must_contain": ["adapters.py"],
                 "must_contain_any": [["cert_verify"], ["urllib3"]]},
         reference="HTTPAdapter.cert_verify 里 verify=False 时把 cert_reqs 设为 "
                   "CERT_NONE 并抑制 urllib3 的 InsecureRequestWarning；"
                   "TLS 握手仍在进行，只是不校验证书链",
         why="常见误解点，需要读 cert_verify 的实现细节。"),

    dict(id="B11", type="B",
         question="Response 有 encoding 和 apparent_encoding 两个属性，它们有什么区别？分别怎么来的？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["apparent_encoding"], ["get_encoding_from_headers"]]},
         reference="encoding 取 Content-Type 里的 charset（utils.get_encoding_from_headers），"
                   "没有则 None（此时 text 走 apparent_encoding 猜测）；"
                   "apparent_encoding 用 chardet/charset_normalizer 检测内容",
         why="两个相似属性，需要分别说明来源。"),

    dict(id="B12", type="B",
         question="requests 的 hook 机制是怎么工作的？可以挂哪些事件？",
         checks={"must_contain": ["hooks.py"],
                 "must_contain_any": [["dispatch_hook"], ["response"]]},
         reference="default_hooks() 定义 response 这一个事件；回调在 "
                   "Session.send 里经 dispatch_hook 调用，可挂单个函数或函数列表",
         why="hooks.py 只有 48 行，但要答全「事件名 + 调用点」。"),

    dict(id="B13", type="B",
         question="requests 的异常体系是怎么组织的？连接超时和读取超时分别对应哪两个异常类？",
         checks={"must_contain": ["exceptions.py"],
                 "must_contain_any": [["RequestException"], ["ConnectTimeout"],
                                      ["ReadTimeout"], ["Timeout"]]},
         reference="全部继承 RequestException；ConnectTimeout 和 ReadTimeout "
                   "是 Timeout 的子类（exceptions.py:91 / 98）",
         why="需要同时答出「继承结构」和「两个具体类」。"),

    dict(id="B14", type="B",
         question="requests 自己实现重试了吗？如果没有，重试是交给谁做的？",
         checks={"must_contain": ["adapters.py"],
                 "must_contain_any": [["max_retries"], ["urllib3"], ["Retry"]]},
         reference="requests 不自己重试；HTTPAdapter 的 max_retries 默认 0，"
                   "传入 urllib3.util.Retry 实例后由 urllib3 在连接层重试",
         why="边界归属问题：能力在 requests 之外。"),

    dict(id="B15", type="B",
         question="PreparedRequest.prepare() 按顺序都做了哪些准备工作？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["prepare_method"], ["prepare_url"],
                                      ["prepare_headers"], ["prepare_body"]]},
         reference="依次 prepare_method -> prepare_url -> prepare_headers -> "
                   "prepare_cookies -> prepare_body -> prepare_auth -> prepare_hooks",
         why="七个步骤的顺序，需要读 prepare 方法体。"),

    dict(id="B16", type="B",
         question="Response.json() 在什么情况下会抛异常？抛出的是 requests 自己的异常吗？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["json"], ["JSONDecodeError"], ["complexjson"]]},
         reference="内容不是合法 JSON 时抛 requests.exceptions.JSONDecodeError，"
                   "它同时继承 InvalidJSONError 和 json.JSONDecodeError（兼容两方 except）",
         why="多重继承设计，需要读 exceptions.py 和 models.py 两处。"),

    dict(id="B17", type="B",
         question="Session.mount() 挂载适配器之后，发请求时是怎么匹配到对应适配器的？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["mount"], ["get_adapter"], ["adapters"]]},
         reference="mount 把前缀写入 self.adapters（OrderedDict）；"
                   "get_adapter 按键长降序找第一个匹配 url 前缀的适配器",
         why="需要读懂「按前缀长度排序」这个细节。"),

    dict(id="B18", type="B",
         question="服务器返回 gzip 压缩的内容时，requests 是在哪儿解压的？解压行为能关掉吗？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["decode_content"], ["iter_content"],
                                      ["Content-Encoding", "content-encoding"]]},
         reference="由 urllib3 在读取时解压；Response.iter_content 的 "
                   "decode_content 参数控制（默认 True），raw.read 也传该参数",
         why="需要找到 decode_content 参数的传递路径。"),

    dict(id="B19", type="B",
         question="Session 上设的 headers 和单次请求里传的 headers，最终怎么合并？谁覆盖谁？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["merge_setting"], ["CaseInsensitiveDict"]]},
         reference="Session.prepare_request 里用 merge_setting(request_headers, "
                   "self.headers, dict_class=CaseInsensitiveDict)，请求级覆盖会话级",
         why="需要读懂 merge_setting 的第二参数是被覆盖方。"),

    dict(id="B20", type="B",
         question="requests.codes 这个东西是怎么实现的？为什么既能用 codes.ok 又能用 codes['ok']？",
         checks={"must_contain": ["status_codes.py"],
                 "must_contain_any": [["LookupDict"], ["_init"], ["structures"]]},
         reference="status_codes.py 末尾 codes = LookupDict(name='status_codes')，"
                   "LookupDict（structures.py:96）的 __getattr__ 把属性访问转到 __getitem__",
         why="需要跨两个文件，理解 __getattr__ 的转发机制。"),
]

# ============================================================================
# C 类：多跳推理（20 条）
#
# 特征：答案分散在三处以上，或者需要「改了 A 会影响谁」这类反向追踪。
#       上下文压力最大 —— 前面查到的每一处都得记住，
#       这正是三种编排策略差异最可能被放大的地方。
# ============================================================================

C = [
    dict(id="C01", type="C",
         question="如果要改变 URL 的 IDNA 编码行为（比如禁用对非 ASCII 域名的编码），需要动哪个函数？还有哪些地方依赖这个行为？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["prepare_url"], ["_get_idna_encoded_host"],
                                      ["idna"]]},
         reference="PreparedRequest.prepare_url（models.py:483）调用 "
                   "_get_idna_encoded_host（models.py:474）；"
                   "依赖方：Session.prepare_request、Request.prepare、api 系列",
         why="需要同时找到实现点和调用方（反向追踪）。"),

    dict(id="C02", type="C",
         question="想给 Session 加一个「会话级默认超时」，让每次请求自动带上。需要改哪几处？请列出具体文件和函数。",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["__init__"], ["request"], ["send"]]},
         reference="Session.__init__（存 self.default_timeout）、"
                   "Session.request（合并到 kwargs）、Session.send（真正传给适配器）；"
                   "注意 merge_environment_settings 也需要考虑",
         why="经典的影响面分析题，要列出三处改动点。"),

    dict(id="C03", type="C",
         question="现在有个异常类叫 RetryError。如果我要新增一个 ProxyTimeoutError（继承 ConnectTimeout），应该加在哪个文件的什么位置？还需要同步改动哪里才能让 requests.xxx 直接访问到？",
         checks={"must_contain": ["exceptions.py"],
                 "must_contain_any": [["ConnectTimeout"], ["__all__", "all"]]},
         reference="加在 requests/exceptions.py 的 ConnectTimeout 之后；"
                   "同时要加到文件末尾的 __all__ 列表里，否则不会被 "
                   "requests/__init__.py 的 from .exceptions import * 导出",
         why="陷阱题：只加类不加 __all__ 是无效的，必须两处都答到。"),

    dict(id="C04", type="C",
         question="一个请求从进入到发出，headers 一共被修改过几次？分别在哪些函数里？",
         checks={"must_contain": ["models.py", "adapters.py"],
                 "must_contain_any": [["prepare_headers"], ["add_headers"],
                                      ["merge_setting"]]},
         reference="① Session.prepare_request 用 merge_setting 合并会话级与请求级；"
                   "② PreparedRequest.prepare_headers 规范化；"
                   "③ HTTPAdapter.add_headers 补默认头；"
                   "④ proxy_headers 处理代理场景",
         why="需要沿调用链数出所有改动点，漏一处就错。"),

    dict(id="C05", type="C",
         question="如果要在请求真正发出前统一埋一个日志点，最合适插在哪个函数？为什么插在那里能覆盖所有请求？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["Session.send", "session.send"], ["send"]]},
         reference="Session.send（sessions.py:752）—— 所有请求（包括 "
                   "Session.get/post 等便捷方法）最终都汇聚到这里；"
                   "api.py 的各函数也只是转调 Session.request",
         why="汇聚点识别，需要确认所有路径都过这一点。"),

    dict(id="C06", type="C",
         question="追踪 verify 参数：从用户传入到最终作用于 SSL 上下文，中间经过了哪些函数？",
         checks={"must_contain": ["sessions.py", "adapters.py"],
                 "must_contain_any": [["merge_environment_settings"], ["cert_verify"],
                                      ["send"]]},
         reference="Session.request -> Session.merge_environment_settings "
                   "-> Session.send(verify=...) -> HTTPAdapter.send -> "
                   "HTTPAdapter.cert_verify（设 cert_reqs / ca_certs）"
                   "-> HTTPAdapter.get_connection_with_tls_context",
         why="参数透传 + 类型转换，跨两个文件五个函数。"),

    dict(id="C07", type="C",
         question="resolve_redirects 被谁调用？如果把它的生成器行为改成一次性返回列表，会影响哪些代码？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["resolve_redirects"], ["send"], ["allow_redirects"]]},
         reference="唯一调用方是 Session.send（sessions.py:752）里 "
                   "if allow_redirects 分支；改成列表后调用方的 for 循环语义不变，"
                   "但会失去逐个 yield 的流式处理与 history 的逐步累积",
         why="反向追踪调用方，并分析语义影响。"),

    dict(id="C08", type="C",
         question="proxies 参数从用户传入，到真正被 urllib3 使用，中间经过了哪些处理函数？环境变量里的代理是在哪一步混进来的？",
         checks={"must_contain": ["sessions.py", "utils.py"],
                 "must_contain_any": [["merge_environment_settings"],
                                      ["get_environ_proxies"], ["select_proxy"],
                                      ["resolve_proxies"]]},
         reference="Session.request -> merge_environment_settings（其中 "
                   "get_environ_proxies 读环境变量）-> Session.send -> "
                   "rebuild_proxies / resolve_proxies -> "
                   "utils.select_proxy -> HTTPAdapter.proxy_manager_for",
         why="五跳链路，且要指出环境变量混入的确切步骤。"),

    dict(id="C09", type="C",
         question="cookie 从服务器响应返回，到下一次请求被自动带上，中间经过了哪些对象和方法？",
         checks={"must_contain": ["cookies.py", "models.py"],
                 "must_contain_any": [["extract_cookies_to_jar"], ["prepare_cookies"],
                                      ["get_cookie_header"]]},
         reference="响应侧：HTTPAdapter.build_response -> "
                   "extract_cookies_to_jar(resp._original_response, ...) 写入 jar；"
                   "请求侧：PreparedRequest.prepare_cookies -> "
                   "get_cookie_header 生成 Cookie 头",
         why="双向追踪，需要串起写入和读出两条路径。"),

    dict(id="C10", type="C",
         question="requests 里哪些地方会读取操作系统环境变量？各读的是什么？",
         checks={"must_contain": ["utils.py"],
                 "must_contain_any": [["get_environ_proxies"], ["proxy_bypass"],
                                      ["get_netrc_auth"]]},
         reference="utils.get_environ_proxies（http_proxy/https_proxy/no_proxy）、"
                   "utils.proxy_bypass、utils.get_netrc_auth（NETRC）、"
                   "Session.merge_environment_settings 中 trust_env 总开关",
         why="需要扫全库找 env 读取点，是典型的广域搜索任务。"),

    dict(id="C11", type="C",
         question="HTTPAdapter.send 的签名如果加一个必填参数，会影响哪些调用方？请列出所有调用点。",
         checks={"must_contain": ["adapters.py"],
                 "must_contain_any": [["BaseAdapter"], ["send"], ["Session.send"]]},
         reference="定义侧：BaseAdapter.send（adapters.py:128，抽象基类）与 "
                   "HTTPAdapter.send（adapters.py:634）；"
                   "调用侧：Session.send（sessions.py:752）里的 adapter.send(...)；"
                   "此外自定义适配器（继承 BaseAdapter）也会受影响",
         why="影响面分析，要同时答定义侧和调用侧。"),

    dict(id="C12", type="C",
         question="响应体的编码处理涉及哪几个函数？从原始字节到 Response.text 中间发生了什么？",
         checks={"must_contain": ["models.py", "utils.py"],
                 "must_contain_any": [["apparent_encoding"], ["stream_decode_response_unicode"],
                                      ["get_encoding_from_headers"], ["iter_content"]]},
         reference="get_encoding_from_headers 取 charset；无 charset 时 "
                   "apparent_encoding 用 chardet 猜；"
                   "iter_content(decode_content=True) 由 urllib3 解压；"
                   "stream_decode_response_unicode（utils.py:594）增量解码；"
                   "最后拼成 Response.text",
         why="跨两文件四个函数，且要讲清顺序。"),

    dict(id="C13", type="C",
         question="如果要把默认的 Accept-Encoding 改掉，需要动哪里？改了之后解压逻辑会受影响吗？",
         checks={"must_contain": ["utils.py"],
                 "must_contain_any": [["default_headers"], ["decode_content"],
                                      ["iter_content"]]},
         reference="utils.default_headers()（utils.py:951）里设置 "
                   "Accept-Encoding；改掉后 iter_content 的 decode_content "
                   "仍按实际 Content-Encoding 解压，两者解耦，但需保证 "
                   "urllib3 支持该编码",
         why="改动点 + 副作用分析，两个问题都要答。"),

    dict(id="C14", type="C",
         question="Session 被 pickle（序列化）时会丢掉哪些东西？为什么要特意丢掉它们？",
         checks={"must_contain": ["sessions.py"],
                 "must_contain_any": [["__getstate__"], ["__setstate__"], ["adapters"]]},
         reference="Session.__getstate__（sessions.py:899）返回 "
                   "{'attrs': {k: v for k, v in self.__dict__.items() if k not in state}}，"
                   "排除 adapters（含 socket/SSL 对象不可序列化）；"
                   "__setstate__ 重建并重新 mount 默认适配器",
         why="需要理解「为什么排除」而不仅是「排除了什么」。"),

    dict(id="C15", type="C",
         question="追踪 data 参数：requests.post(url, data={...}) 传进去的字典，最终变成什么？中间经过哪些转换？",
         checks={"must_contain": ["models.py", "utils.py"],
                 "must_contain_any": [["prepare_body"], ["_encode_params"],
                                      ["prepare_content_length"]]},
         reference="Session.request -> Request.prepare -> "
                   "PreparedRequest.prepare_body（models.py:576）："
                   "非空 data 走 _encode_params（models.py:134 / utils 相关）"
                   "urlencode 成表单串；同时 prepare_content_length 设置 "
                   "Content-Length；files 存在时改用 multipart",
         why="跨文件追踪 + 分支（files 存在时行为不同）。"),

    dict(id="C16", type="C",
         question="iter_lines 和 iter_content 是什么关系？iter_lines 是怎么在字节流上做行切分的？",
         checks={"must_contain": ["models.py"],
                 "must_contain_any": [["iter_lines"], ["iter_content"], ["splitlines"]]},
         reference="iter_lines（models.py:980）内部调用 iter_content，"
                   "维护 pending buffer 拼接不完整行，用 splitlines() 切分，"
                   "保留最后一段留待下一块",
         why="需要读懂 buffer 拼接逻辑，比单纯定位难。"),

    dict(id="C17", type="C",
         question="哪些地方可能抛出 Timeout 相关的异常？它们分别对应连接阶段还是读取阶段？",
         checks={"must_contain": ["adapters.py", "exceptions.py"],
                 "must_contain_any": [["ConnectTimeout"], ["ReadTimeout"], ["send"]]},
         reference="HTTPAdapter.send 捕获 urllib3 的 "
                   "ConnectTimeoutError -> requests.ConnectTimeout；"
                   "ReadTimeoutError -> requests.ReadTimeout；"
                   "两个类都定义在 exceptions.py（91 / 98），同继承 Timeout",
         why="异常转换点 + 类型定义，需要跨两文件。"),

    dict(id="C18", type="C",
         question="HTTPAdapter 里跟连接有关的方法有几个？请列出它们并说明各自职责。",
         checks={"must_contain": ["adapters.py"],
                 "must_contain_any": [["init_poolmanager"], ["get_connection"],
                                      ["get_connection_with_tls_context"],
                                      ["close"]]},
         reference="init_poolmanager（建池，239）、proxy_manager_for（代理池，269）、"
                   "get_connection_with_tls_context（按 TLS 上下文取连接，455）、"
                   "get_connection（对外接口，512）、close（关闭清理，555）",
         why="广域枚举 + 逐个说明，上下文压力最大的一类。"),

    dict(id="C19", type="C",
         question="requests 顶层的 requests.get / requests.post 这些便捷函数是怎么实现的？它们和直接用 Session 有什么区别和联系？",
         checks={"must_contain": ["api.py", "sessions.py"],
                 "must_contain_any": [["session"], ["Session"], ["request"]]},
         reference="api.py 每个函数用 with sessions.Session() as session: "
                   "return session.request(...)，即每次新建 Session；"
                   "所以顶层函数无法复用连接池和 cookie，"
                   "等价于一次性 Session",
         why="需要指出「每次新建 Session」这个关键差异。"),

    dict(id="C20", type="C",
         question="如果要把整个 requests 的异常基类换一个新的父类，需要检查哪些地方？请列出所有可能的受影响点。",
         checks={"must_contain": ["exceptions.py"],
                 "must_contain_any": [["RequestException"], ["__all__"], ["IOError", "OSError"]]},
         reference="RequestException（exceptions.py:20）当前继承 IOError；"
                   "受影响点：① 其余全部异常类的继承链；② 文件末尾 __all__；"
                   "③ 用户代码里的 except IOError / OSError 捕获；"
                   "④ models.py / adapters.py 中的 raise 点仍正常",
         why="影响面最广的一题，要区分「必须改」和「不用改」。"),
]

ALL = A + B + C


# ============================================================================
# 自检：断言里的每个符号，必须在仓库里真实存在
# ============================================================================

def _files_text():
    """返回 {相对路径: 全文}。

    断言里刻意只写短文件名（sessions.py 而不是 requests/sessions.py）：
    模型回答时两种写法都可能出现，用短名匹配两者都覆盖得到
    （"requests/sessions.py" 里包含 "sessions.py"）。
    所以自检时也要按短名匹配。
    """
    texts = {}
    for dirpath, _, files in os.walk(REPO):
        for fn in files:
            if fn.endswith(".py"):
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, REPO).replace(os.sep, "/")
                with open(p, encoding="utf-8", errors="replace") as f:
                    texts[rel] = f.read()
    return texts


def _by_basename(texts):
    """{短文件名: [相对路径, ...]}"""
    out = {}
    for rel in texts:
        out.setdefault(os.path.basename(rel), []).append(rel)
    return out


def selfcheck(verbose=True):
    """校验每一条：**符号确实存在，且确实在断言指定的那个文件里**。

    这是整套实验的地基。标准答案写错一条，就会有一个任务
    无论 agent 答得多好都判不及格 —— 而且你完全看不出来。
    """
    texts = _files_text()
    base = _by_basename(texts)
    problems = []

    def _files_for(shortname):
        """短文件名 -> 该文件全文（可能多个同名字段，拼接）"""
        return "\n".join(texts[r] for r in base.get(shortname, []))

    for t in ALL:
        tid = t["id"]
        checks = t.get("checks") or {}

        if not checks:
            problems.append(f"{tid}: 没有任何断言")
            continue

        # must_contain 里既可能是文件名也可能是符号名，分别处理
        must = checks.get("must_contain", [])
        files_named = [m for m in must if m.endswith(".py")]
        symbols = [m for m in must
                   if not m.endswith(".py") and re.fullmatch(r"[A-Za-z_][\w]*", m)]

        for f in files_named:
            if f not in base:
                problems.append(f"{tid}: 断言里的文件 {f} 仓库中不存在")

        for sym in symbols:
            # 符号必须出现在至少一个 must_contain 指定的文件里
            if not files_named:
                # 没指定文件时，至少要在整个仓库里能找到
                if not any(sym in txt for txt in texts.values()):
                    problems.append(f"{tid}: 符号 {sym} 在整个仓库中都不存在")
            else:
                hit = [f for f in files_named if sym in _files_for(f)]
                if not hit:
                    where = [f for f, txt in texts.items() if sym in txt]
                    hint = f"（实际出现在 {where[:3]}）" if where else "（全仓库都没有）"
                    problems.append(
                        f"{tid}: 符号 {sym} 不在断言指定的文件 {files_named} 里{hint}")

        # must_contain_any：每个 group 至少要有一项在仓库里存在
        for group in checks.get("must_contain_any", []):
            plain = [g for g in group if re.fullmatch(r"[A-Za-z_][\w]*", g)]
            if plain and not any(any(g in txt for txt in texts.values()) for g in plain):
                problems.append(f"{tid}: must_contain_any 组 {group} 中没有任何一项在仓库里存在")

        # 正则要能编译
        for pat in checks.get("regex", []):
            try:
                re.compile(pat)
            except re.error as e:
                problems.append(f"{tid}: 正则不合法 {pat} -> {e}")

    # id 唯一
    ids = [t["id"] for t in ALL]
    if len(ids) != len(set(ids)):
        dup = [i for i in set(ids) if ids.count(i) > 1]
        problems.append(f"id 重复: {dup}")

    if verbose:
        print(f"评测集自检：{len(ALL)} 条")
        print(f"  A 类（精确查找）: {len(A)}")
        print(f"  B 类（语义理解）: {len(B)}")
        print(f"  C 类（多跳推理）: {len(C)}")
        print(f"  仓库: {REPO}")
        print("-" * 62)
        if problems:
            print(f"发现 {len(problems)} 个问题：")
            for p in problems:
                print("  ✗", p)
        else:
            print("全部通过：每条断言里的符号都真实存在于断言指定的文件中")
        print("-" * 62)

    return problems


def build(out=OUT):
    with open(out, "w", encoding="utf-8") as f:
        for t in ALL:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    problems = selfcheck()
    if "--check" in sys.argv:
        sys.exit(1 if problems else 0)
    if problems:
        print("\n评测集有问题，拒绝生成 tasks.jsonl —— 修完再跑。")
        sys.exit(1)
    path = build()
    print(f"已生成 {path}  （{len(ALL)} 条）")
