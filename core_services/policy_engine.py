"""
Titan-1 Policy Engine
Stores agent trajectories, trains k-NN policy models, and runs inference.
"""

import os
import json
import pickle
import logging
import hashlib
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager
from datetime import datetime

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import LabelEncoder
from sklearn.feature_extraction.text import TfidfVectorizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("policy_engine")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trajectories.db")
MODEL_STORE_PATH = Path(os.getenv("MODEL_STORE_PATH", "./policy_models"))
MODEL_STORE_PATH.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


#  ORM 

class TrajectoryRecord(Base):
    __tablename__ = "trajectories"

    id          = Column(Integer, primary_key=True, index=True)
    policy_type = Column(String(128), index=True, nullable=False)
    input_text  = Column(Text, nullable=False)
    action      = Column(String(256), nullable=False)
    reward      = Column(Float, default=0.0)
    metadata    = Column(Text, default="{}")
    created_at  = Column(DateTime, default=datetime.utcnow)


class PolicyModelRecord(Base):
    __tablename__ = "policy_models"

    id          = Column(Integer, primary_key=True, index=True)
    policy_type = Column(String(128), unique=True, index=True, nullable=False)
    artifact    = Column(String(512), nullable=False)   # path to .pkl
    sample_count= Column(Integer, default=0)
    trained_at  = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Policy Engine starting up")
    yield
    logger.info("Policy Engine shutting down")


app = FastAPI(title="Titan-1 Policy Engine", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


#  Schemas 

class TrajectoryItem(BaseModel):
    input_text: str
    action: str
    reward: float = 0.0
    metadata: dict = {}

class TrainRequest(BaseModel):
    policy_type: str = Field(..., description="Identifier for the policy (e.g. 'code_gen', 'search')")
    trajectory: list[TrajectoryItem]
    retrain: bool = Field(default=True, description="Merge with existing data and retrain")

class TrainResponse(BaseModel):
    policy_type: str
    sample_count: int
    model_path: str
    status: str

class InferRequest(BaseModel):
    policy_type: str
    input_text: str
    top_k: int = Field(default=1, ge=1, le=10)

class InferResponse(BaseModel):
    policy_type: str
    predicted_action: str
    confidence: float
    alternatives: list[dict]


#  Model helpers 

def _model_path(policy_type: str) -> Path:
    safe = hashlib.md5(policy_type.encode()).hexdigest()[:12]
    return MODEL_STORE_PATH / f"{safe}_{policy_type.replace('/', '_')}.pkl"


def _train_knn(inputs: list[str], actions: list[str], rewards: list[float]) -> tuple:
    """Vectorize inputs with TF-IDF and train a weighted k-NN classifier."""
    vectorizer = TfidfVectorizer(max_features=512, ngram_range=(1, 2))
    X = vectorizer.fit_transform(inputs).toarray()

    # Weight samples by reward (clip to positive)
    weights = np.clip(np.array(rewards) + 1.0, 0.01, None)
    sample_weight = weights / weights.sum()

    le = LabelEncoder()
    y = le.fit_transform(actions)

    n_neighbors = min(5, len(y))
    clf = KNeighborsClassifier(n_neighbors=n_neighbors, metric="cosine", weights="distance")
    clf.fit(X, y, sample_weight=sample_weight)

    return {"vectorizer": vectorizer, "classifier": clf, "label_encoder": le}


def _save_model(policy_type: str, bundle: dict, sample_count: int, db: Session):
    path = _model_path(policy_type)
    with open(path, "wb") as f:
        pickle.dump(bundle, f)

    existing = db.query(PolicyModelRecord).filter_by(policy_type=policy_type).first()
    if existing:
        existing.artifact = str(path)
        existing.sample_count = sample_count
        existing.trained_at = datetime.utcnow()
    else:
        db.add(PolicyModelRecord(
            policy_type=policy_type,
            artifact=str(path),
            sample_count=sample_count,
        ))
    db.commit()
    return path


def _load_model(policy_type: str, db: Session) -> dict:
    record = db.query(PolicyModelRecord).filter_by(policy_type=policy_type).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"No trained model for policy_type='{policy_type}'")
    path = Path(record.artifact)
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Model artifact missing at {path}")
    with open(path, "rb") as f:
        return pickle.load(f)


#  Routes 

@app.get("/health")
def health():
    return {"status": "ok", "service": "policy_engine"}


@app.post("/policies/train", response_model=TrainResponse)
def train(req: TrainRequest):
    db: Session = SessionLocal()
    try:
        # Persist incoming trajectories
        for t in req.trajectory:
            db.add(TrajectoryRecord(
                policy_type=req.policy_type,
                input_text=t.input_text,
                action=t.action,
                reward=t.reward,
                metadata=json.dumps(t.metadata),
            ))
        db.commit()

        # Load all stored trajectories for this policy_type (merge)
        records = db.query(TrajectoryRecord).filter_by(policy_type=req.policy_type).all()
        inputs  = [r.input_text for r in records]
        actions = [r.action     for r in records]
        rewards = [r.reward     for r in records]

        if len(inputs) < 2:
            return TrainResponse(
                policy_type=req.policy_type,
                sample_count=len(inputs),
                model_path="",
                status="deferred  need >=2 samples to train",
            )

        bundle = _train_knn(inputs, actions, rewards)
        path   = _save_model(req.policy_type, bundle, len(inputs), db)

        logger.info("Trained %s on %d samples -> %s", req.policy_type, len(inputs), path)
        return TrainResponse(
            policy_type=req.policy_type,
            sample_count=len(inputs),
            model_path=str(path),
            status="trained",
        )
    finally:
        db.close()


@app.post("/policies/infer", response_model=InferResponse)
def infer(req: InferRequest):
    db: Session = SessionLocal()
    try:
        bundle = _load_model(req.policy_type, db)

        vec: TfidfVectorizer  = bundle["vectorizer"]
        clf: KNeighborsClassifier = bundle["classifier"]
        le:  LabelEncoder     = bundle["label_encoder"]

        X = vec.transform([req.input_text]).toarray()

        # Probabilities across known classes
        proba = clf.predict_proba(X)[0]
        top_k_idx = np.argsort(proba)[::-1][: req.top_k]

        top_action    = le.inverse_transform([top_k_idx[0]])[0]
        top_confidence = float(proba[top_k_idx[0]])

        alternatives = [
            {"action": le.inverse_transform([i])[0], "confidence": float(proba[i])}
            for i in top_k_idx[1:]
        ]

        return InferResponse(
            policy_type=req.policy_type,
            predicted_action=top_action,
            confidence=top_confidence,
            alternatives=alternatives,
        )
    finally:
        db.close()


@app.get("/policies")
def list_policies():
    db: Session = SessionLocal()
    try:
        records = db.query(PolicyModelRecord).all()
        return {
            "policies": [
                {
                    "policy_type": r.policy_type,
                    "sample_count": r.sample_count,
                    "trained_at": r.trained_at.isoformat() if r.trained_at else None,
                }
                for r in records
            ]
        }
    finally:
        db.close()


@app.get("/policies/{policy_type}/trajectories")
def get_trajectories(policy_type: str, limit: int = 100):
    db: Session = SessionLocal()
    try:
        records = (
            db.query(TrajectoryRecord)
            .filter_by(policy_type=policy_type)
            .order_by(TrajectoryRecord.created_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "policy_type": policy_type,
            "count": len(records),
            "trajectories": [
                {
                    "id": r.id,
                    "input_text": r.input_text,
                    "action": r.action,
                    "reward": r.reward,
                    "created_at": r.created_at.isoformat(),
                }
                for r in records
            ],
        }
    finally:
        db.close()
