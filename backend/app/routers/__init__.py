"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import candidate_evidence, resource

# 专用路由含固定前缀 /api/candidate-evidence，必须在动态 /api/{module} 之前注册。
ROUTERS = [candidate_evidence, resource]
