"""Known 1C global namespaces used by notebook and Worker binding.

Worker modules historically admit a few additional platform namespaces.
Keep that wider set explicit so sharing the registry does not change notebook
lowering for other qualified names.
"""

from __future__ import annotations


NOTEBOOK_PLATFORM_GLOBALS = frozenset({
    "статуссообщения",
    "режимзаписидокумента",
    "символы",
    "кодировкатекста",
    "справочники",
    "документы",
    "журналыдокументов",
    "регистрысведений",
    "регистрынакопления",
    "регистрыбухгалтерии",
    "регистрырасчета",
    "планывидовхарактеристик",
    "планысчетов",
    "планывидоврасчета",
    "планыобмена",
    "бизнеспроцессы",
    "задачи",
    "критерииотбора",
    "последовательности",
    "константы",
    "перечисления",
    "внешниеобработки",
    "внешниеотчеты",
    "обработки",
    "отчеты",
    "метаданные",
    "параметрысеанса",
})

WORKER_PLATFORM_GLOBALS = NOTEBOOK_PLATFORM_GLOBALS | frozenset({
    "частидаты",
    "обходрезультатазапроса",
    "видсравнениякомпоновкиданных",
    "типгруппыэлементовотборакомпоновкиданных",
    "цветастиля",
})
