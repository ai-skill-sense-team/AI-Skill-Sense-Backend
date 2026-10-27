
from datetime import datetime, timezone

from database.dependencies import get_db
from database.models import (
    HealthConnectDevice,
    User,
    UserLog,
    UserQuest,
    UserSkill,
    UserWebhookCredential,
)
from datapool.dependencies import get_datapool_db
from datapool.models import Quest, Skill, SkillQuest
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from schemas.user import (
    UserAssignmentRequest,
    UserAssignmentResponse,
    UserLogCreateRequest,
    UserLogResponse,
    UserLogsResponse,
    UserProfileResponse,
    UserProfileWriteRequest,
    UserProfileWriteResponse,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from routers.auth import get_current_user
from services.user_logs import create_user_log

router = APIRouter(
    prefix="/api/users",
    tags=["Users"],
)

PROFILE_FIELDS = (
    "age",
    "gender",
    "interests",
    "summary",
    "occupation",
    "experience",
    "education_status",
    "education_summary",
)


class QuestProgressUpdate(BaseModel):
    delta: float = Field(gt=0, le=1)


def _profile_has_data(user: User) -> bool:
    return any(
        value not in (None, [], "")
        for value in (getattr(user, field) for field in PROFILE_FIELDS)
    )


@router.get("/{user_id}/dashboard")
def get_user_dashboard(
    user_id: str,
    db: Session = Depends(get_db),
    datapool_db: Session = Depends(get_datapool_db),
    current_user: User | None = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user access token is required.",
        )
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot access another user's dashboard.",
        )

    user_skills = (
        db.query(UserSkill.skill_id)
        .filter(UserSkill.user_id == user_id)
        .all()
    )
    skill_ids = [skill_id for (skill_id,) in user_skills]
    skills = (
        datapool_db.query(Skill)
        .filter(Skill.skill_id.in_(skill_ids))
        .all()
        if skill_ids
        else []
    )

    user_quests = (
        db.query(UserQuest)
        .filter(UserQuest.user_id == user_id)
        .order_by(UserQuest.started_at.desc())
        .all()
    )
    quest_ids = [user_quest.quest_id for user_quest in user_quests]
    quests = (
        datapool_db.query(Quest)
        .filter(Quest.quest_id.in_(quest_ids))
        .all()
        if quest_ids
        else []
    )
    quest_by_id = {quest.quest_id: quest for quest in quests}

    linked_skills_by_quest: dict[str, list[str]] = {}
    if skill_ids and quest_ids:
        for skill_id, quest_id in (
            datapool_db.query(SkillQuest.skill_id, SkillQuest.quest_id)
            .filter(
                SkillQuest.skill_id.in_(skill_ids),
                SkillQuest.quest_id.in_(quest_ids),
            )
            .all()
        ):
            linked_skills_by_quest.setdefault(quest_id, []).append(skill_id)

    connected_health = (
        db.query(HealthConnectDevice.user_id)
        .filter(HealthConnectDevice.user_id == user_id)
        .first()
        is not None
    )
    connected_webhook = (
        db.query(UserWebhookCredential.user_id)
        .filter(UserWebhookCredential.user_id == user_id)
        .first()
        is not None
    )
    completed_count = sum(
        user_quest.status == "completed" for user_quest in user_quests
    )

    return {
        "user_id": current_user.user_id,
        "username": current_user.username,
        "email": current_user.email,
        "total_xp": completed_count * 150,
        "fitness_connected": connected_health,
        "webhook_connected": connected_webhook,
        "skills": [
            {
                "skill_id": skill.skill_id,
                "skill_type": skill.skill_type,
                "skill_title": skill.skill_title,
                "skill_intro": skill.skill_intro or "",
            }
            for skill in skills
        ],
        "quests": [
            {
                "quest_id": user_quest.quest_id,
                "linked_skill": linked_skills_by_quest.get(
                    user_quest.quest_id,
                    [],
                ),
                "quest_name": quest_by_id[user_quest.quest_id].quest_title,
                "quest_desc": quest_by_id[user_quest.quest_id].quest_intro or "",
                "quest_type": quest_by_id[user_quest.quest_id].quest_type,
                "scale": quest_by_id[user_quest.quest_id].scale,
                "progress": user_quest.progress,
                "quest_metrics": quest_by_id[user_quest.quest_id].quest_metrics,
                "allocation_time": user_quest.started_at,
                "duration": (
                    (user_quest.duration_ms or quest_by_id[user_quest.quest_id].duration_ms)
                    / 60000
                ),
                "status": user_quest.status,
            }
            for user_quest in user_quests
            if user_quest.quest_id in quest_by_id
        ],
    }


@router.post("/{user_id}/quests/{quest_id}/progress")
def update_user_quest_progress(
    user_id: str,
    quest_id: str,
    request: QuestProgressUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user access token is required.",
        )
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot update another user's quest.",
        )

    assignment = (
        db.query(UserQuest)
        .filter(
            UserQuest.user_id == user_id,
            UserQuest.quest_id == quest_id,
        )
        .first()
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Quest assignment not found.",
        )
    if assignment.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active quests can receive progress.",
        )

    assignment.progress = min(1.0, assignment.progress + request.delta)
    assignment.updated_at = datetime.now(timezone.utc)
    completed = assignment.progress >= 1.0
    if completed:
        assignment.status = "completed"
        assignment.completed_at = assignment.updated_at
        create_user_log(
            db,
            background_tasks,
            user_id,
            "quest_completion",
            f"Completed quest {quest_id}.",
            event_id=f"manual-quest-completion:{quest_id}",
        )
    else:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    return {
        "quest_id": quest_id,
        "progress": assignment.progress,
        "status": assignment.status,
        "completed": completed,
    }


@router.get("/{user_id}/profile", response_model=UserProfileResponse)
def get_user_profile(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot access another user's profile.",
        )

    user = db.query(User).filter(User.user_id == current_user.user_id).first()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    skills = (
        db.query(UserSkill.skill_id)
        .filter(UserSkill.user_id == current_user.user_id)
        .all()
    )

    quests = (
        db.query(UserQuest.quest_id, UserQuest.status)
        .filter(UserQuest.user_id == current_user.user_id)
        .all()
    )

    skill_ids = [skill_id for (skill_id,) in skills]
    active_quests = [
        quest_id for quest_id, quest_status in quests
        if quest_status == "active"
    ]
    completed_quests = [
        quest_id for quest_id, quest_status in quests
        if quest_status == "completed"
    ]
    failed_quests = [
        quest_id for quest_id, quest_status in quests
        if quest_status == "failed"
    ]

    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "age": user.age,
        "gender": user.gender,
        "interests": user.interests or [],
        "skills": skill_ids,
        "active_quests": active_quests,
        "completed_quest": completed_quests,
        "failed_quests": failed_quests,
        "summary": user.summary,
        "occupation": user.occupation,
        "experience": user.experience,
        "education_status": user.education_status,
        "education_summary": user.education_summary,
    }


@router.post(
    "/{user_id}/profile",
    response_model=UserProfileWriteResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user_profile(
    user_id: str,
    request: UserProfileWriteRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user access token is required.",
        )
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot create another user's profile.",
        )
    profile_user = db.query(User).filter(User.user_id == user_id).first()
    if profile_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    if _profile_has_data(profile_user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user profile already exists; use the profile update route.",
        )

    profile_data = request.model_dump(exclude_unset=True)
    if not any(value not in (None, [], "") for value in profile_data.values()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one non-empty profile field is required.",
        )

    for field, value in profile_data.items():
        setattr(profile_user, field, value)

    create_user_log(
        db,
        background_tasks,
        profile_user.user_id,
        "new_profile",
        "Created user profile.",
    )
    return {"status": "created", "user_id": profile_user.user_id}


@router.put(
    "/{user_id}/profile",
    response_model=UserProfileWriteResponse,
)
def update_user_profile(
    user_id: str,
    request: UserProfileWriteRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user access token is required.",
        )
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot update another user's profile.",
        )
    profile_user = db.query(User).filter(User.user_id == user_id).first()
    if profile_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    if not _profile_has_data(profile_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Create the user profile before updating it.",
        )

    profile_data = request.model_dump(exclude_unset=True)
    if not profile_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one profile field must be provided.",
        )

    for field, value in profile_data.items():
        setattr(profile_user, field, value)

    create_user_log(
        db,
        background_tasks,
        profile_user.user_id,
        "profile_update",
        "Updated user profile.",
    )
    return {"status": "updated", "user_id": profile_user.user_id}


@router.get("/{user_id}/logs", response_model=UserLogsResponse)
def get_user_logs(
    user_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot access another user's logs.",
        )

    logs = (
        db.query(UserLog)
        .filter(UserLog.user_id == current_user.user_id)
        .order_by(UserLog.timestamp.desc())
        .limit(limit)
        .all()
    )

    return {
        "logs": [
            {
                "user_id": log.user_id,
                "log_id": log.log_id,
                "log_desc": log.log_desc,
                "action_required": "Yes" if log.action_required else "No",
                "timestamp": log.timestamp,
            }
            for log in logs
        ]
    }


@router.post(
    "/{user_id}/logs",
    response_model=UserLogResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user_log_route(
    user_id: str,
    request: UserLogCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user access token is required.",
        )
    if user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot create another user's logs.",
        )

    log = create_user_log(
        db,
        background_tasks,
        current_user.user_id,
        request.activity_type,
        request.log_desc,
    )
    return {
        "user_id": log.user_id,
        "log_id": log.log_id,
        "log_desc": log.log_desc,
        "action_required": "Yes" if log.action_required else "No",
        "timestamp": log.timestamp,
    }


@router.post(
    "/{user_id}/assignments",
    response_model=UserAssignmentResponse,
)
def assign_user_recommendation(
    user_id: str,
    request: UserAssignmentRequest,
    background_tasks: BackgroundTasks, # Added background_tasks parameter
    db: Session = Depends(get_db),
    datapool_db: Session = Depends(get_datapool_db),
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A user or agent credential is required.",
        )

    if request.assignment_type == "Skill":
        item = (
            datapool_db.query(Skill)
            .filter(Skill.skill_id == request.item_id)
            .first()
        )
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Skill not found in the DataPool.",
            )

        assignment = (
            db.query(UserSkill)
            .filter(
                UserSkill.user_id == user_id,
                UserSkill.skill_id == request.item_id,
            )
            .first()
        )
        if assignment is not None:
            return {
                "status": "already_assigned",
                "user_id": user_id,
                "assignment_type": request.assignment_type,
                "item_id": request.item_id,
            }

        db.add(UserSkill(user_id=user_id, skill_id=request.item_id))
        
        # FIX: Trigger log for new skill assignment
        create_user_log(
            db,
            background_tasks,
            user_id,
            "new_skill",
            f"Assigned new skill {request.item_id}.",
            event_id=f"skill-assignment:{request.item_id}",
        )
    else:
        item = (
            datapool_db.query(Quest)
            .filter(Quest.quest_id == request.item_id)
            .first()
        )
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Quest not found in the DataPool.",
            )

        assignment = (
            db.query(UserQuest)
            .filter(
                UserQuest.user_id == user_id,
                UserQuest.quest_id == request.item_id,
            )
            .first()
        )
        if assignment is not None:
            return {
                "status": "already_assigned",
                "user_id": user_id,
                "assignment_type": request.assignment_type,
                "item_id": request.item_id,
            }

        db.add(
            UserQuest(
                user_id=user_id,
                quest_id=request.item_id,
                duration_ms=item.duration_ms,
                progress=0.0,
                status="active",
            )
        )
        
        # FIX: Trigger log for new quest assignment
        create_user_log(
            db,
            background_tasks,
            user_id,
            "new_quest",
            f"Assigned new quest {request.item_id}.",
            event_id=f"quest-assignment:{request.item_id}",
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if request.assignment_type == "Skill":
            existing = (
                db.query(UserSkill)
                .filter(
                    UserSkill.user_id == user_id,
                    UserSkill.skill_id == request.item_id,
                )
                .first()
            )
        else:
            existing = (
                db.query(UserQuest)
                .filter(
                    UserQuest.user_id == user_id,
                    UserQuest.quest_id == request.item_id,
                )
                .first()
            )
        if existing is None:
            raise
        return {
            "status": "already_assigned",
            "user_id": user_id,
            "assignment_type": request.assignment_type,
            "item_id": request.item_id,
        }
    except Exception:
        db.rollback()
        raise

    return {
        "status": "assigned",
        "user_id": user_id,
        "assignment_type": request.assignment_type,
        "item_id": request.item_id,
    }