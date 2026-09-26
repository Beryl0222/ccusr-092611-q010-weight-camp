# 减重训练风险台

面向减重训练营的风险与结算服务：管理报名、医学/运动风险评估、训练日程、红旗冻结、
转诊、暂停与退款决定，保证教练、授权医务人员、财务看到各自被授权的信息。

## 业务规则

- **排课门禁**：医学评估与运动风险评估都签署后才可排课；冻结、暂停、转诊未闭环、
  营期取消同样阻断排课与打卡。
- **评估不可覆盖**：评估一经签署即锁定（按学员+类型唯一）；后续材料只能通过
  “补交资料”追加，任何补交都不会改动已签署内容（内容哈希可核对）。
- **红旗冻结**：出现红旗症状（胸闷、晕厥、心悸等）立即冻结该学员全部未开始的课程，
  并通知医务人员与教练；学员仍想继续也无法打卡。**只有授权医务人员可以解除冻结**，
  解冻时课程恢复为已排状态。
- **转诊闭环**：仅医务人员可发起/闭环转诊；转诊期间阻断训练，闭环后回到在训状态
  （冻结是否解除仍由医务人员单独决定）。
- **幂等结算**：所有写命令要求携带 `request_key`；重复打卡或重复退款请求不会产生
  第二笔账。台账表对业务引用有唯一约束，并发打卡也只会成功一次。
- **角色化视图**：`GET /enrollees/{id}/view` 按角色投影（教练看不到医学评估正文与
  账目；账目仅财务/管理员/学员本人可见），并输出该角色**当前可执行动作清单**
  （含禁止原因），可逐条核对。
- **可追溯与恢复**：全部决定写入哈希链审计表（SQLite 触发器禁止改/删），通知带
  送达状态；进程重启、重开数据库文件后，决定依据、通知状态、审计链均可重算校验；
  营期取消后历史同样保留。

## 角色

`owner`（学员本人）、`coach`（教练）、`medic`（授权医务人员）、`finance`（财务）、
`admin`（营期管理员）。HTTP 请求以 `X-User-Id` / `X-User-Role` 头表明身份。

## HTTP 接口

写命令均为 `POST /<command>`，JSON 体必带 `request_key`；相同 `request_key` 的
重试返回首次结果。

| 路径 | 说明 |
| --- | --- |
| `POST /camps` | 建营 |
| `POST /enrollments` | 报名 |
| `POST /assessments` | 签署医学/运动风险评估（medic 签 medical，coach 签 exercise） |
| `POST /supplements` | 补交资料（只追加，不覆盖评估） |
| `POST /sessions` | 排课（门禁不通过返回 409） |
| `POST /red-flags` | 上报红旗，冻结后续课程并通知 medic/coach |
| `POST /unfreeze` | 医务人员解除冻结 |
| `POST /pause` / `POST /resume` | 暂停 / 恢复 |
| `POST /referrals` / `POST /referral-resolutions` | 发起 / 闭环转诊 |
| `POST /checkins` | 打卡并结算（重复打卡不重复扣费） |
| `POST /refund-requests` / `POST /refund-decisions` | 退款请求（学员/管理员）/ 退款决定（财务） |
| `POST /camp-cancellations` | 管理员取消营期，未开始课程全部取消并通知各角色 |
| `POST /notification-receipts` | 按角色签收通知 |
| `GET /enrollees/{id}/view` | 角色化视图 + `gates` + `actions` |
| `GET /enrollees?camp_id=` | 学员名单（仅员工） |
| `GET /notifications` | 本角色通知（学员队列按本人隔离） |
| `GET /audit?enrollee_id=` | 哈希链操作历史 |
| `GET /health` | 服务状态与审计链校验结果 |

启动示例：

```python
from weight_camp.api import serve
serve(host="127.0.0.1", port=8080, database="camp.db")
```

## 目录

- src/weight_camp/domain.py：角色、评估类型、红旗症状与通知订阅约定。
- src/weight_camp/service.py：SQLite 事务、状态门禁、权限、幂等结算、哈希链审计。
- src/weight_camp/api.py：本地 HTTP 接口与角色化输出。
- tests/：门禁、冻结权限、转诊、重复/并发结算、角色视图、重启恢复、防篡改与 API 测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
