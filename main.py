import base64
import pickle
import os.path
import logging
import re
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError
from email.mime.text import MIMEText


# Configurable variables
SENDER_EMAIL = ""  # Replace with your email
UNCHALLENGED_LABEL_ID = ""
CHALLENGED_LABEL_ID = ""
PASSED_LABEL_ID = ""
UNCHALLENGED_LABEL = "unchallenged"
CHALLENGED_LABEL = "challenged"
PASSED_LABEL = "passed"
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# Regular expression pattern for affirmative responses
AFFIRMATIVE_REGEX = r"yes|yea|yeah|yah|definitely|absolutely|certainly|i think so|of course"

# Set up logging
logging.basicConfig(level=logging.DEBUG, filename='app.log', filemode='w',
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Function to get Gmail service
def get_gmail_service():
    logging.info("Setting up the Gmail service")
    creds = None
    # The file token.pickle stores the user's access and refresh tokens.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
            logging.info("Loaded credentials from token.pickle")
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES, redirect_uri='http://localhost:52158/')
            creds = flow.run_local_server(port=52158)
        # Save the credentials for the next run.
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
            logging.info("Saved new credentials to token.pickle")

    service = build('gmail', 'v1', credentials=creds)
    return service

# Function to create an email message
def create_message(sender, to, subject, message_text):
    message = MIMEText(message_text)
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    return {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}

# Function to send an email message
def send_message(service, user_id, message, recipient):
    try:
        sent_message = service.users().messages().send(userId=user_id, body=message).execute()
        logging.info(f"Sent message to {recipient}")
        return sent_message
    except HttpError as error:
        logging.error(f'An error occurred when sending a message: {error}')
        return None

# Function to process emails
def process_emails(service):
    try:
        logging.info("Starting to process emails")
        # Fetching unchallenged emails
        query = f'label:{UNCHALLENGED_LABEL} -label:{PASSED_LABEL}'
        unchallenged_results = service.users().messages().list(userId='me', q=query).execute()
        unchallenged_messages = unchallenged_results.get('messages', [])
        logging.debug(f"Fetched {len(unchallenged_messages)} unchallenged emails")

        for message in unchallenged_messages:
            original_email_id = message['id']  # Store the ID of the original email
            msg = service.users().messages().get(userId='me', id=original_email_id).execute()

            # Extract sender and subject from headers
            sender, subject = None, None
            for header in msg['payload']['headers']:
                if header['name'] == 'From':
                    sender = header['value']
                elif header['name'] == 'Subject':
                    subject = header['value']

            if sender is None or subject is None:
                logging.warning(f"Unable to find sender or subject for message ID {original_email_id}")
                continue

            thread_id = msg['threadId']

            # Check if the thread already has the challenged label
            thread_labels = set(service.users().threads().get(userId='me', id=thread_id).execute().get('messages', [])[0]['labelIds'])
            if CHALLENGED_LABEL_ID in thread_labels:
                # Remove the unchallenged label if it exists
                if UNCHALLENGED_LABEL_ID in thread_labels:
                    service.users().threads().modify(
                        userId='me',
                        id=thread_id,
                        body={
                            'removeLabelIds': [UNCHALLENGED_LABEL_ID]
                        }
                    ).execute()
                    logging.info(f"Removed 'unchallenged' label from thread {thread_id}")
                logging.info(f"Thread {thread_id} already has the challenged label. Skipping.")
                continue

            # Check if there are emails from the same sender with a label of "passed"
            passed_senders_query = f'label:{PASSED_LABEL} from:{sender}'
            passed_results = service.users().messages().list(userId='me', q=passed_senders_query).execute()
            if passed_results.get('messages'):
                logging.info(f"Thread {thread_id} has emails from the same sender with a label of 'passed'. Skipping challenge.")

                # Modify labels for the thread
                thread = service.users().threads().get(userId='me', id=thread_id).execute()
                for email in thread['messages']:
                    service.users().messages().modify(
                        userId='me',
                        id=email['id'],
                        body={
                            'addLabelIds': ['INBOX',PASSED_LABEL_ID],
                            'removeLabelIds': [UNCHALLENGED_LABEL_ID]
                        }
                    ).execute()
                    logging.info(f"Modified labels for message ID {email['id']} in thread {thread_id}")
                continue

            # Sending challenge email
            challenge_subject = "Re: " + subject
            body = "Are you a human?"
            reply = create_message(SENDER_EMAIL, sender, challenge_subject, body)
            reply['threadId'] = thread_id
            challenge_message = send_message(service, 'me', reply, sender)
            logging.info(f"Challenge sent to {sender} for message ID {original_email_id}")

            # Modify labels for the thread
            thread = service.users().threads().get(userId='me', id=thread_id).execute()
            for email in thread['messages']:
                service.users().messages().modify(
                    userId='me',
                    id=email['id'],
                    body={
                        'addLabelIds': [CHALLENGED_LABEL_ID],
                        'removeLabelIds': [UNCHALLENGED_LABEL_ID]
                    }
                ).execute()
                logging.info(f"Modified labels for message ID {email['id']} in thread {thread_id}")

        # Processing replies to challenged emails
        replies_results = service.users().messages().list(userId='me', labelIds=[CHALLENGED_LABEL_ID]).execute()
        challenged_messages = replies_results.get('messages', [])

        for message in challenged_messages:
            thread = service.users().threads().get(userId='me', id=message['threadId']).execute()
            all_messages = thread['messages']

            # Assuming the first message in the thread is the original email
            original_message_time = int(all_messages[0]['internalDate'])

            for index, msg in enumerate(all_messages):
                if index == 0:
                    continue  # Skip the first message (original email)

                # Check if the reply contains an affirmative response
                reply_text = msg['snippet'].lower()
                if re.search(AFFIRMATIVE_REGEX, reply_text):
                    # Modify labels for the original message in the thread
                    service.users().messages().modify(
                        userId='me',
                        id=thread['messages'][0]['id'],  # The first message in the thread
                        body={
                            'addLabelIds': ['INBOX', PASSED_LABEL_ID],
                        }
                    ).execute()
                    logging.info(f"Email in thread {thread['id']} has passed the challenge.")
                    break
            for email in thread['messages']:
                service.users().messages().modify(
                    userId='me',
                    id=email['id'],
                    body={
                        'removeLabelIds': [UNCHALLENGED_LABEL_ID]
                    }
                ).execute()
        logging.info("Finished processing emails")

    except HttpError as error:
        logging.error(f'An error occurred: {error}')


# Main function
def main():
    service = get_gmail_service()
    process_emails(service)

# Entry point of the script
if __name__ == '__main__':
    main()
