# Copyright 2023 BentoML Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
import logging
import typing as t

import openllm

from .configuration_llama import DEFAULT_PROMPT_TEMPLATE
from ..._llm import LLMEmbeddings
from ..._prompt import default_formatter


if t.TYPE_CHECKING:
    import torch
    import torch.nn.functional as F

    import transformers
else:
    transformers = openllm.utils.LazyLoader("transformers", globals(), "transformers")
    torch = openllm.utils.LazyLoader("torch", globals(), "torch")
    F = openllm.utils.LazyLoader("F", globals(), "torch.nn.functional")


logger = logging.getLogger(__name__)


class LlaMA(openllm.LLM["transformers.LlamaForCausalLM", "transformers.LlamaTokenizerFast"]):
    __openllm_internal__ = True

    def sanitize_parameters(
        self,
        prompt: str,
        top_k: int | None = None,
        top_p: float | None = None,
        temperature: float | None = None,
        max_new_tokens: int | None = None,
        use_default_prompt_template: bool = True,
        use_llama2_prompt: bool = True,
        **attrs: t.Any,
    ) -> tuple[str, dict[str, t.Any], dict[str, t.Any]]:
        if use_default_prompt_template:
            _PROMPT = DEFAULT_PROMPT_TEMPLATE("v2" if use_llama2_prompt else "v1")
            template_variables = default_formatter.extract_template_variables(_PROMPT)
            prompt_variables = {k: v for k, v in attrs.items() if k in template_variables}
            if "instruction" in prompt_variables:
                raise RuntimeError(
                    "'instruction' should be passed as the first argument "
                    "instead of kwargs when 'use_default_prompt_template=True'"
                )
            try:
                prompt_text = _PROMPT.format(instruction=prompt, **prompt_variables)
            except KeyError as e:
                raise RuntimeError(
                    f"Missing variable '{e.args[0]}' (required: {template_variables}) in the prompt template. "
                    "Use 'use_default_prompt_template=False' to disable the default prompt template."
                ) from None
        else:
            prompt_text = prompt

        generation_config = {
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }

        return prompt_text, generation_config, {}

    @property
    def import_kwargs(self):
        model_kwds = {"device_map": "auto" if torch.cuda.device_count() > 1 else None}
        tokenizer_kwds: dict[str, t.Any] = {}
        return model_kwds, tokenizer_kwds

    def postprocess_generate(self, prompt: str, generation_result: list[str], **_: t.Any) -> str:
        return generation_result[0]

    def generate(self, prompt: str, **attrs: t.Any) -> list[str]:
        from ..._generation import StopOnTokens

        generation_kwargs = {
            "generation_config": self.config.model_construct_env(**attrs).to_generation_config(),
            "stopping_criteria": transformers.StoppingCriteriaList([StopOnTokens()]),
        }

        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            gen_tokens = self.model.generate(**inputs, **generation_kwargs)
            return self.tokenizer.batch_decode(gen_tokens, skip_special_tokens=True, clean_up_tokenization_spaces=True)

    def embeddings(self, prompts: list[str]) -> LLMEmbeddings:
        encoding = self.tokenizer(prompts, padding=True, return_tensors="pt").to(self.device)
        input_ids = encoding["input_ids"]
        attention_mask = encoding["attention_mask"]
        with torch.inference_mode():
            model_outputs = self.model(input_ids=input_ids, attention_mask=attention_mask, output_hidden_states=True)
            data = model_outputs.hidden_states[-1]
            mask = attention_mask.unsqueeze(-1).expand(data.size()).float()
            masked_embeddings = data * mask
            sum_embeddings = torch.sum(masked_embeddings, dim=1)
            seq_length = torch.sum(mask, dim=1)
            embedding = sum_embeddings / seq_length
            normalized_embeddings = F.normalize(embedding, p=2, dim=1)
        return {
            "embeddings": normalized_embeddings,
            "num_tokens": torch.sum(attention_mask).item(),
        }