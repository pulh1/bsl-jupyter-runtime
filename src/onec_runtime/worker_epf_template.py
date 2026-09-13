from __future__ import annotations

"""Readable fixed metadata for the one-object-module runtime Worker."""


WORKER_FILE_UUID = "2a00a4fa-8ea9-4dc4-9de1-472044c40101"
WORKER_OBJECT_UUID = "2a00a4fa-8ea9-4dc4-9de1-472044c40102"
WORKER_MODULE_STREAM = f"{WORKER_OBJECT_UUID}.0"

WORKER_METADATA = """{1,
{2a00a4fa-8ea9-4dc4-9de1-472044c40101},1,
{c3831ec8-d8d5-4f93-8a22-f9bfae07327f,
{1,
{4,2a00a4fa-8ea9-4dc4-9de1-472044c40103,2a00a4fa-8ea9-4dc4-9de1-472044c40104,
{0,
{3,
{1,0,2a00a4fa-8ea9-4dc4-9de1-472044c40102},"Worker",
{1,"ru","Runtime Worker"},"",0,0,00000000-0000-0000-0000-000000000000,0}
},00000000-0000-0000-0000-000000000000,"",00000000-0000-0000-0000-000000000000},4,
{2bcef0d1-0981-11d6-b9b8-0050bae0a95d,0},
{3daea016-69b7-4ed4-9453-127911372fe6,0},
{d5b0e5ed-256d-401c-9c36-f630cafd8a62,0},
{ec6bb5e5-b7a8-4d75-bec9-658107a699cf,0}
}
}
}"""

WORKER_COPYINFO = """{4,
{0},
{0},
{0},
{0,0},
{0}
}"""

WORKER_ROOT = "{2,2a00a4fa-8ea9-4dc4-9de1-472044c40101,}"

WORKER_VERSION = """{
{216,0,
{80327,0}
}
}"""

WORKER_MODULE_INFO = '{3,1,0,"",0}'

WORKER_STREAM_NAMES = (
    WORKER_FILE_UUID,
    WORKER_MODULE_STREAM,
    "copyinfo",
    "root",
    "version",
    "versions",
)
