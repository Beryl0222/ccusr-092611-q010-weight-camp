# 减重训练风险台

面向减重训练营的风险管控本地服务：管理评估签署、训练日程、红旗冻结、转诊与退款决定，
向教练、医务人员、财务人员输出差异化信息，并保留可校验、不可篡改的操作历史。

## 核心规则

- **评估门禁**：医学评估（医务人员签署）与运动风险评估（教练签署）全部完成前，报名不能排入训练。
- **评估不可覆盖**：评估一经签署即固化；重复签署和“补交资料覆盖”一律拒绝，补交资料另立留痕。
- **红旗冻结**：训练中报告胸闷/晕厥等红旗症状后，立即冻结该学员当前及后续全部课程，
  并向医务、教练、管理员发出通知；学员坚持继续也不能打卡或排课。
- **解冻授权**：只有授权医务人员（构造时登记）可解除冻结；转诊进行中须先关闭转诊。
- **幂等结算**：打卡、退款请求、退款结算等写操作以 `request_key` 幂等；
  重复打卡不会产生第二条考勤，重复退款请求/结算不会重复结算。
- **营期取消**：管理员可取消整个训练营，未结束报名全部置为 cancelled，未开始课程取消。
- **可追溯**：全部决定与状态迁移写入 SHA-256 哈希链事件表，事件含决定依据（症状、冻结编号、
  转诊原因等）；进程重启后自动重放校验，任何库内篡改都会导致打开失败。
- **动作核对**：`GET /trainees/{id}/actions` 返回该操作人当前每项动作是否允许及禁止原因，
  与服务端实际强制保持一致。
- **角色隔离**：病史、禁忌用药仅医务人员/管理员可见；财务只见缴费与退款；评估正文仅签署角色与管理员可见。

## 目录

- `src/weight_camp/domain.py`：角色、评估类型、报名状态、红旗症状、通知角色矩阵。
- `src/weight_camp/service.py`：SQLite 事务、状态迁移、权限、幂等、哈希链与角色视图。
  `CampService` 为业务实现；`DomainStore` 为保留的通用状态机骨架。
- `src/weight_camp/api.py`：本地 HTTP 接口（`X-Actor-Id` / `X-Actor-Role` 头鉴权）。
- `tests/`：门禁、冻结、通知角色、不可覆盖、幂等、授权解冻、取消、重启保留、篡改检测、
  动作投影与 HTTP 冒烟测试。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /trainees` | 报名（body 含 profile，可携带病史/缴费金额） |
| `POST /trainees/{id}/assessments` | 签署 `medical` / `fitness` 评估（不可重复） |
| `POST /trainees/{id}/supplements` | 补交资料（不覆盖已签署评估） |
| `POST /sessions` | 排课（评估未完成或冻结中拒绝） |
| `POST /sessions/{id}/check-in` | 打卡（重复提交幂等/拒绝） |
| `POST /sessions/{id}/red-flag` | 上报红旗，冻结后续课程并发通知 |
| `POST /trainees/{id}/lift-freeze` | 授权医务人员解冻 |
| `POST /trainees/{id}/refer` / `referral/resolve` | 发起/关闭转诊 |
| `POST /trainees/{id}` | 申请退款 |
| `POST /trainees/{id}/refund/settle` | 财务批准结算或拒绝（只结算一次） |
| `POST /camp/cancel` | 管理员取消训练营 |
| `GET /trainees/{id}` | 按角色裁剪的学员视图 |
| `GET /trainees/{id}/actions` | 当前可执行动作核对 |
| `GET /trainees/{id}/history` | 带哈希的操作历史（敏感正文按角色遮蔽） |
| `GET /notifications` | 本角色通知及送达状态；`POST /notifications/{id}/deliver` |
| `GET /verify` | 重放校验哈希链 |

写操作建议在 body 中携带唯一 `request_key` 抵御重复提交。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests

## 启动

    WEIGHT_CAMP_DB=camp.db WEIGHT_CAMP_MEDICS=medic-1,medic-2 \
      PYTHONPATH=src python3 -m weight_camp.api
