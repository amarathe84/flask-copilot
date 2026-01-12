#!/usr/bin/env python3
"""
Example script demonstrating MariaDB connection for saving experiment state.
"""

import pymysql
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# Database connection string
# Format: mysql+pymysql://username:password@localhost/database
DATABASE_URL = "mysql+pymysql://flask_user:flask_password@localhost/flask_experiments"

# Create SQLAlchemy engine
engine = create_engine(DATABASE_URL, echo=False)
Base = declarative_base()

# Define an example Experiment model
class Experiment(Base):
    __tablename__ = 'experiments'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    parameters = Column(JSON)  # Store experiment parameters as JSON
    results = Column(JSON)     # Store results as JSON
    status = Column(String(50))  # e.g., 'running', 'completed', 'failed'
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

def init_database():
    """Initialize the database tables."""
    Base.metadata.create_all(engine)
    print("✓ Database tables created successfully!")

def save_experiment(name, description, parameters, results=None, status='pending'):
    """Save an experiment to the database."""
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        experiment = Experiment(
            name=name,
            description=description,
            parameters=parameters,
            results=results,
            status=status
        )
        session.add(experiment)
        session.commit()
        print(f"✓ Experiment '{name}' saved with ID: {experiment.id}")
        return experiment.id
    except Exception as e:
        session.rollback()
        print(f"✗ Error saving experiment: {e}")
        return None
    finally:
        session.close()

def get_all_experiments():
    """Retrieve all experiments from the database."""
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        experiments = session.query(Experiment).all()
        return experiments
    finally:
        session.close()

def update_experiment_status(experiment_id, status, results=None):
    """Update an experiment's status and results."""
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        experiment = session.query(Experiment).filter_by(id=experiment_id).first()
        if experiment:
            experiment.status = status
            if results:
                experiment.results = results
            experiment.updated_at = datetime.utcnow()
            session.commit()
            print(f"✓ Experiment {experiment_id} updated to status: {status}")
            return True
        else:
            print(f"✗ Experiment {experiment_id} not found")
            return False
    except Exception as e:
        session.rollback()
        print(f"✗ Error updating experiment: {e}")
        return False
    finally:
        session.close()

if __name__ == "__main__":
    print("=== MariaDB Connection Example ===\n")
    
    # Initialize database
    init_database()
    
    # Example: Save a new experiment
    print("\n--- Saving Example Experiment ---")
    exp_id = save_experiment(
        name="Lead Molecule Optimization Test",
        description="Testing LMO with vLLM backend",
        parameters={
            "model": "meta-llama/Llama-3.3-70B-Instruct",
            "backend": "livai",
            "max_iterations": 5,
            "target_molecule": "CCO"
        },
        status="running"
    )
    
    # Example: Update experiment status
    if exp_id:
        print("\n--- Updating Experiment Status ---")
        update_experiment_status(
            exp_id,
            status="completed",
            results={
                "optimized_molecule": "CC(C)O",
                "iterations": 3,
                "final_score": 0.87
            }
        )
    
    # Example: Retrieve all experiments
    print("\n--- All Experiments ---")
    experiments = get_all_experiments()
    for exp in experiments:
        print(f"ID: {exp.id}, Name: {exp.name}, Status: {exp.status}")
    
    print("\n✓ Done! Database connection working successfully.")
