"""
Authentication Middleware for FastAPI
Validates sessions stored in MySQL by Node.js express-session
"""
import json
import os
from datetime import datetime
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
import mysql.connector
from db_config import get_db_connection
from urllib.parse import unquote

# Local dev bypass - set LOCAL_DEV=true in .env to skip auth
LOCAL_DEV = os.getenv("LOCAL_DEV", "").lower() == "true"
LOCAL_DEV_USER = {
    "id": 1,
    "username": "dev_user",
    "full_name": "Local Developer",
    "role": "admin",
    "realm": "division",
    "div_office_code": "CSMT-ML",
    "can_access_rtis": True
}

def parse_session_cookie(cookie_header: str) -> Optional[str]:
    """
    Extract session ID from connect.sid cookie

    Args:
        cookie_header: Full Cookie header string

    Returns:
        Session ID without signature, or None if not found
    """
    if not cookie_header:
        return None

    # Parse cookies
    cookies = {}
    for cookie in cookie_header.split(';'):
        cookie = cookie.strip()
        if '=' in cookie:
            key, value = cookie.split('=', 1)
            cookies[key] = value

    # Get connect.sid cookie
    session_cookie = cookies.get('connect.sid')
    if not session_cookie:
        return None

    # express-session format: s:SESSION_ID.SIGNATURE
    # We need the SESSION_ID part
    if session_cookie.startswith('s:'):
        session_cookie = session_cookie[2:]  # Remove 's:' prefix

    # Split by dot to separate session_id from signature
    if '.' in session_cookie:
        session_id = session_cookie.split('.')[0]
        return session_id

    return session_cookie


def get_session_from_mysql(session_id: str) -> Optional[dict]:
    """
    Retrieve and parse session data from MySQL

    Args:
        session_id: Session ID from cookie

    Returns:
        Parsed session data dict, or None if not found/expired
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Query session from database
        cursor.execute(
            """
            SELECT session_id, expires, data
            FROM sessions
            WHERE session_id = %s
            LIMIT 1
            """,
            (session_id,)
        )

        row = cursor.fetchone()
        cursor.close()

        if not row:
            return None

        # Check if expired
        expires_sec = int(row["expires"])
        now_sec = int(datetime.now().timestamp())

        if now_sec > expires_sec:
            return None             

        # Parse session data (JSON string)
        try:
            session_data = json.loads(row['data'])
            return session_data
        except json.JSONDecodeError:
            print(f"[AUTH] Failed to parse session data: {row['data'][:100]}")
            return None

    except mysql.connector.Error as e:
        print(f"[AUTH] MySQL error: {e}")
        return None
    except Exception as e:
        print(f"[AUTH] Unexpected error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def extract_session_id_from_connect_sid(connect_sid: str | None) -> str | None:
    if not connect_sid:
        return None

    decoded = unquote(connect_sid)          # s%3A... -> s:...
    if decoded.startswith("s:"):
        decoded = decoded[2:]               # remove "s:"
    sid = decoded.split(".", 1)[0].strip()  # remove signature
    return sid or None

def get_current_user(request: Request) -> Optional[dict]:
    """
    Get current logged-in user from session

    Args:
        request: FastAPI Request object

    Returns:
        User dict with: id, username, full_name, role, realm, div_office_code, can_access_rtis
        Or None if not authenticated
    """
    # Local dev bypass
    if LOCAL_DEV:
        print("[AUTH] LOCAL_DEV mode - bypassing authentication")
        return LOCAL_DEV_USER

    print("[AUTH] raw cookie connect.sid =", request.cookies.get("connect.sid"))
    # Get cookie header
    cookie_header = request.headers.get('cookie')
    if not cookie_header:
        return None

    # Extract session ID
    # session_id = parse_session_cookie(cookie_header)

    session_id = extract_session_id_from_connect_sid(request.cookies.get("connect.sid"))
    print("[AUTH] extracted session_id =", session_id)
    if not session_id:
        return None

    # Get session from MySQL
    session_data = get_session_from_mysql(session_id)
    print("[AUTH] session_data type =", type(session_data))
    print("[AUTH] session_data =", session_data)
    if not session_data:
        return None

    # Extract user from session
    user = session_data.get('user')
    if not user:
        return None
    
    # Enforce RTIS access flag from DB (authoritative)
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT can_access_rtis FROM users WHERE id=%s LIMIT 1", (user["id"],))
    row = cur.fetchone()
    user["can_access_rtis"] = bool(row["can_access_rtis"])
    cur.close()
    conn.close()

    if not row or not row.get("can_access_rtis"):
      return None

    return user


def require_auth(request: Request) -> dict:
    """
    Require authentication - raise exception if not logged in

    Args:
        request: FastAPI Request object

    Returns:
        User dict

    Raises:
        HTTPException: 401 if not authenticated
    """
    user = get_current_user(request)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please log in.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return user


def require_rtis_access(request: Request) -> dict:
    """
    Require RTIS access permission - raise exception if not authorized

    Args:
        request: FastAPI Request object

    Returns:
        User dict

    Raises:
        HTTPException: 401 if not authenticated, 403 if no RTIS access
    """
    user = require_auth(request)

    # Check RTIS permission in database
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT can_access_rtis
            FROM users
            WHERE id = %s
            LIMIT 1
            """,
            (user['id'],)
        )

        row = cursor.fetchone()
        cursor.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User not found in database"
            )

        if not row.get('can_access_rtis'):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access RTIS Analysis Tool. Please contact your administrator."
            )

        return user

    except mysql.connector.Error as e:
        print(f"[AUTH] MySQL error checking permissions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify permissions"
        )
    finally:
        if conn:
            conn.close()


def check_realm(user: dict, allowed_realms: list) -> bool:
    """
    Check if user belongs to allowed realm(s)

    Args:
        user: User dict from get_current_user()
        allowed_realms: List of allowed realms (e.g., ['division', 'suburban'])

    Returns:
        True if user's realm is in allowed list
    """
    user_realm = user.get('realm', '').lower()
    return user_realm in [realm.lower() for realm in allowed_realms]


# FastAPI Dependency for use with Depends()
async def get_authenticated_user(request: Request) -> dict:
    """
    FastAPI dependency for authenticated routes

    Usage:
        @app.get("/protected")
        async def protected_route(user = Depends(get_authenticated_user)):
            return {"message": f"Hello {user['username']}"}
    """
    return require_auth(request)


async def get_rtis_user(request: Request) -> dict:
    """
    FastAPI dependency for RTIS-protected routes

    Usage:
        @app.post("/load_csv")
        async def load_csv(file: UploadFile, user = Depends(get_rtis_user)):
            # Only users with can_access_rtis=TRUE can access this
            return {"user": user['username']}
    """
    return require_rtis_access(request)
