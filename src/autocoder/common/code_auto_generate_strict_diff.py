from typing import List, Dict, Tuple
from autocoder.common.types import Mode, CodeGenerateResult
from autocoder.common import AutoCoderArgs
import byzerllm
from autocoder.utils.queue_communicate import queue_communicate, CommunicateEvent, CommunicateEventType
from autocoder.common import sys_prompt
from concurrent.futures import ThreadPoolExecutor
import json
from autocoder.common.utils_code_auto_generate import chat_with_continue,stream_chat_with_continue,ChatWithContinueResult
from autocoder.utils.auto_coder_utils.chat_stream_out import stream_out
from autocoder.common.stream_out_type import CodeGenerateStreamOutType
from autocoder.common.auto_coder_lang import get_message_with_format
from autocoder.common.printer import Printer
from autocoder.rag.token_counter import count_tokens
from autocoder.utils import llms as llm_utils
from autocoder.common import SourceCodeList
from autocoder.privacy.model_filter import ModelPathFilter
from autocoder.memory.active_context_manager import ActiveContextManager
from autocoder.common.rulefiles.autocoderrules_utils import get_rules
from autocoder.run_context import get_run_context,RunMode

class CodeAutoGenerateStrictDiff:
    def __init__(
        self, llm: byzerllm.ByzerLLM, args: AutoCoderArgs, action=None
    ) -> None:
        self.llm = llm
        self.args = args
        self.action = action
        self.llms = []
        self.generate_times_same_model = args.generate_times_same_model
        if not self.llm:
            raise ValueError(
                "Please provide a valid model instance to use for code generation."
            )        
        self.llms = self.llm.get_sub_client("code_model") or [self.llm]
        if not isinstance(self.llms, list):
            self.llms = [self.llms]

    @byzerllm.prompt(llm=lambda self: self.llm)
    def multi_round_instruction(
        self, instruction: str, content: str, context: str = "", package_context: str = ""
    ) -> str:
        """
        如果你需要生成代码，对于每个需要更改的文件，写出类似于 unified diff 的更改，就像`diff -U0`会产生的那样。
        下面是一些生成diff的要求：
        Make sure you include the first 2 lines with the file paths.
        Don't include timestamps with the file paths.

        Start each hunk of changes with a `@@ ... @@` line.
        Must include line numbers like `diff -U0` does.
        The user's patch tool need them.

        The user's patch tool needs CORRECT patches that apply cleanly against the current contents of the file!
        Think carefully and make sure you include and mark all lines that need to be removed or changed as `-` lines.
        Make sure you mark all new or modified lines with `+`.
        Don't leave out any lines or the diff patch won't apply correctly.

        Indentation matters in the diffs!

        To make a new file, show a diff from `--- /dev/null` to `+++ path/to/new/file.ext`.

        下面我们来看一个例子：

        当前项目目录结构：
        1. 项目根目录： /tmp/projects/mathweb
        2. 项目子目录/文件列表(类似tree 命令输出)
        flask/
            app.py
            templates/
                index.html
            static/
                style.css

        用户需求： 请将下面的代码中的is_prime()函数替换为sympy。
        回答：
        好的，我会先罗列出需要的修改步骤，然后再列出diff。
        修改步骤：
        1. 添加sympy的import 语句。
        2. 删除is_prime()函数。
        3. 将现有对is_prime()的调用替换为sympy.isprime()。

        下面是这些变更的diff：

        ```diff
        --- /tmp/projects/mathweb/flask/app.py
        +++ /tmp/projects/mathweb/flask/app.py
        @@ ... @@
        -class MathWeb:
        +import sympy
        +
        +class MathWeb:
        @@ ... @@
        -def is_prime(x):
        -    if x < 2:
        -        return False
        -    for i in range(2, int(math.sqrt(x)) + 1):
        -        if x % i == 0:
        -            return False
        -    return True
        @@ ... @@
        -@app.route('/prime/<int:n>')
        -def nth_prime(n):
        -    count = 0
        -    num = 1
        -    while count < n:
        -        num += 1
        -        if is_prime(num):
        -            count += 1
        -    return str(num)
        +@app.route('/prime/<int:n>')
        +def nth_prime(n):
        +    count = 0
        +    num = 1
        +    while count < n:
        +        num += 1
        +        if sympy.isprime(num):
        +            count += 1
        +    return str(num)
        ```

        现在让我们开始一个新的任务:

        {%- if structure %}
        ====
        {{ structure }}
        {%- endif %}

        {%- if content %}
        ====
        下面是一些文件路径以及每个文件对应的源码：
        <files>
        {{ content }}
        </files>
        {%- endif %}

        {%- if package_context %}
        ====
        下面是上面文件的一些信息（包括最近的变更情况）：
        <package_context>
        {{ package_context }}
        </package_context>
        {%- endif %}

        {%- if context %}
        <extra_context>
        {{ context }}
        </extra_context>
        {%- endif %}

        {%- if extra_docs %}
        ====

        RULES PROVIDED BY USER

        The following rules are provided by the user, and you must follow them strictly.

        {% for key, value in extra_docs.items() %}
        <user_rule>
        ##File: {{ key }}
        {{ value }}
        </user_rule>
        {% endfor %}        
        {% endif %}

        ====

        下面是用户的需求：

        {{ instruction }}

        每次生成一个文件的diff，然后询问我是否继续，当我回复继续，继续生成下一个文件的diff。当没有后续任务时，请回复 "__完成__" 或者 "__EOF__"。
        """
        
        if not self.args.include_project_structure:
            return {
                "structure": "",                
            }
        
        extra_docs = get_rules()

        return {
            "structure": (
                self.action.pp.get_tree_like_directory_structure()
                if self.action
                else ""
            ),
            "extra_docs": extra_docs,
        }

    @byzerllm.prompt(llm=lambda self: self.llm)
    def single_round_instruction(
        self, instruction: str, content: str, context: str = "", package_context: str = ""
    ) -> str:
        """
        如果你需要生成代码，对于每个需要更改的文件，写出类似于 unified diff 的更改，就像`diff -U0`会产生的那样。
        下面是一些生成diff的要求：
        Make sure you include the first 2 lines with the file paths.
        Don't include timestamps with the file paths.

        Start each hunk of changes with a `@@ ... @@` line.
        Must include line numbers like `diff -U0` does.
        The user's patch tool need them.

        The user's patch tool needs CORRECT patches that apply cleanly against the current contents of the file!
        Think carefully and make sure you include and mark all lines that need to be removed or changed as `-` lines.
        Make sure you mark all new or modified lines with `+`.
        Don't leave out any lines or the diff patch won't apply correctly.

        Indentation matters in the diffs!

        To make a new file, show a diff from `--- /dev/null` to `+++ path/to/new/file.ext`.
        The code part of the diff content should not contains any line number.

        The path start with `---` or `+++` should be the absolute path of the file or relative path from the project root.

        下面我们来看一个例子：

        当前项目目录结构：
        1. 项目根目录： /tmp/projects/mathweb
        2. 项目子目录/文件列表(类似tree 命令输出)
        flask/
            app.py
            templates/
                index.html
            static/
                style.css

        用户需求： 请将下面的代码中的is_prime()函数替换为sympy。
        回答：
        好的，我会先罗列出需要的修改步骤，然后再列出diff。
        修改步骤：
        1. 添加sympy的import 语句。
        2. 删除is_prime()函数。
        3. 将现有对is_prime()的调用替换为sympy.isprime()。

        下面是这些变更的diff：

        ```diff
        --- /tmp/projects/mathweb/flask/app.py
        +++ /tmp/projects/mathweb/flask/app.py
        @@ ... @@
        -class MathWeb:
        +import sympy
        +
        +class MathWeb:
        @@ ... @@
        -def is_prime(x):
        -    if x < 2:
        -        return False
        -    for i in range(2, int(math.sqrt(x)) + 1):
        -        if x % i == 0:
        -            return False
        -    return True
        @@ ... @@
        -@app.route('/prime/<int:n>')
        -def nth_prime(n):
        -    count = 0
        -    num = 1
        -    while count < n:
        -        num += 1
        -        if is_prime(num):
        -            count += 1
        -    return str(num)
        +@app.route('/prime/<int:n>')
        +def nth_prime(n):
        +    count = 0
        +    num = 1
        +    while count < n:
        +        num += 1
        +        if sympy.isprime(num):
        +            count += 1
        +    return str(num)
        ```

        现在让我们开始一个新的任务:

        {%- if structure %}
        {{ structure }}
        {%- endif %}

        {%- if content %}
        下面是一些文件路径以及每个文件对应的源码：
        <files>
        {{ content }}
        </files>
        {%- endif %}

        {%- if package_context %}
        下面是上面文件的一些信息（包括最近的变更情况）：
        <package_context>
        {{ package_context }}
        </package_context>
        {%- endif %}

        {%- if context %}
        <extra_context>
        {{ context }}
        </extra_context>
        {%- endif %}

        下面是用户的需求：

        {{ instruction }}
        """
        
        if not self.args.include_project_structure:
            return {
                "structure": "",                
            }

        return {
            "structure": (
                self.action.pp.get_tree_like_directory_structure()
                if self.action
                else ""
            )
        }

    def single_round_run(
        self, query: str, source_code_list: SourceCodeList
    ) -> CodeGenerateResult:
        llm_config = {"human_as_model": self.args.human_as_model}
        source_content = source_code_list.to_str()

        # 获取包上下文信息
        package_context = ""
        
        if self.args.enable_active_context and self.args.enable_active_context_in_generate:
            # 初始化活动上下文管理器
            active_context_manager = ActiveContextManager(self.llm, self.args.source_dir)
            # 获取活动上下文信息
            result = active_context_manager.load_active_contexts_for_files(
                [source.module_name for source in source_code_list.sources]
            )
            # 将活动上下文信息格式化为文本
            if result.contexts:
                package_context_parts = []
                for dir_path, context in result.contexts.items():
                    package_context_parts.append(f"<package_info>{context.content}</package_info>")
                
                package_context = "\n".join(package_context_parts)

        if self.args.template == "common":
            init_prompt = self.single_round_instruction.prompt(
                instruction=query, content=source_content, context=self.args.context,
                package_context=package_context
            )
        elif self.args.template == "auto_implement":
            init_prompt = self.auto_implement_function.prompt(
                instruction=query, content=source_content
            )

        with open(self.args.target_file, "w",encoding="utf-8") as file:
            file.write(init_prompt)

        conversations = []

        if self.args.system_prompt and self.args.system_prompt.strip() == "claude":
            conversations.append(
                {"role": "system", "content": sys_prompt.claude_sys_prompt.prompt()})
        elif self.args.system_prompt:
            conversations.append(
                {"role": "system", "content": self.args.system_prompt})

        conversations.append({"role": "user", "content": init_prompt})
     
        
        conversations_list = []
        results = []
        input_tokens_count = 0
        generated_tokens_count = 0
        input_tokens_cost = 0
        generated_tokens_cost = 0
        model_names = []

        printer = Printer()
        estimated_input_tokens = count_tokens(json.dumps(conversations, ensure_ascii=False))
        printer.print_in_terminal("estimated_input_tokens_in_generate", style="yellow",
                                  estimated_input_tokens_in_generate=estimated_input_tokens,
                                  generate_mode="strict_diff"
                                  )

        if not self.args.human_as_model or get_run_context().mode == RunMode.WEB:
            with ThreadPoolExecutor(max_workers=len(self.llms) * self.generate_times_same_model) as executor:
                futures = []
                count = 0
                for llm in self.llms:
                    for _ in range(self.generate_times_same_model):
                        
                        model_names_list = llm_utils.get_llm_names(llm)
                        model_name = None
                        if model_names_list:
                            model_name = model_names_list[0]                                                    
                        
                        for _ in range(self.generate_times_same_model):
                            model_names.append(model_name)
                            if count == 0:
                                def job():
                                    stream_generator = stream_chat_with_continue(
                                        llm=llm, 
                                        conversations=conversations, 
                                        llm_config=llm_config,
                                        args=self.args
                                    )
                                    full_response, last_meta = stream_out(
                                    stream_generator,
                                    model_name=model_name,
                                    title=get_message_with_format(
                                        "code_generate_title", model_name=model_name),
                                    args=self.args,
                                    extra_meta={
                                        "stream_out_type": CodeGenerateStreamOutType.CODE_GENERATE.value
                                    })
                                    return ChatWithContinueResult(
                                        content=full_response,
                                        input_tokens_count=last_meta.input_tokens_count,
                                        generated_tokens_count=last_meta.generated_tokens_count
                                    )
                                futures.append(executor.submit(job))
                            else:                                
                                futures.append(executor.submit(
                                    chat_with_continue, 
                                    llm=llm, 
                                    conversations=conversations, 
                                    llm_config=llm_config,
                                    args=self.args
                                ))
                            count += 1
                temp_results = [future.result() for future in futures]
                for result in temp_results:
                    results.append(result.content)
                    input_tokens_count += result.input_tokens_count
                    generated_tokens_count += result.generated_tokens_count
                    model_info = llm_utils.get_model_info(model_name, self.args.product_mode)
                    input_cost = model_info.get("input_price", 0) if model_info else 0
                    output_cost = model_info.get("output_price", 0) if model_info else 0
                    input_tokens_cost += input_cost * result.input_tokens_count / 1000000
                    generated_tokens_cost += output_cost * result.generated_tokens_count / 1000000
            for result in results:
                conversations_list.append(
                    conversations + [{"role": "assistant", "content": result}])
        else:            
            for _ in range(self.args.human_model_num):
                single_result = chat_with_continue(
                    llm=self.llms[0], 
                    conversations=conversations, 
                    llm_config=llm_config,
                    args=self.args
                )                
                results.append(single_result.content)
                input_tokens_count += single_result.input_tokens_count
                generated_tokens_count += single_result.generated_tokens_count
                conversations_list.append(conversations + [{"role": "assistant", "content": single_result.content}])
        
        statistics = {
            "input_tokens_count": input_tokens_count,
            "generated_tokens_count": generated_tokens_count,
            "input_tokens_cost": input_tokens_cost,
            "generated_tokens_cost": generated_tokens_cost
        }        

        return CodeGenerateResult(contents=results, conversations=conversations_list, metadata=statistics)
    