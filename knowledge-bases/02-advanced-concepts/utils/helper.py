import json
import re
import ragas
from dataclasses import dataclass, field
import os
import boto3
import time
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import sagemaker
from sagemaker.predictor import Predictor
from sagemaker.base_serializers import JSONSerializer
from sagemaker.base_deserializers import JSONDeserializer


llm_prompt_template = """

Human: Given the following texts, find the best answer to the user question. The accuracy of the answer is critical. If the answer is not found in the context, please say I do not know. Give your answer in just a couple of sentences. Do not give long answer.

####
{{documents}}
####


User Question: {{query}}

Assistant:"""

def generate_questions(bedrock_runtime, model_id, documents):

    prompt_template = """

Human: You are a professor. Your task is to setup \
1 question for an upcoming \
quiz/examination based on the given document wrapped in <document></document> XML tag. \
The question should be diverse in nature \
across the document. The question should not contain options, not start with Q1/ Q2. \
Restrict the question to the context information provided.\

<document>
{{document}}
</document>

Think step by step and pay attention to the number of question to create.

Your response should follow the format as followed:

Question: question
Answer: answer


Assistant:"""
    
    # prompt = prompt_template.replace("{{num_questions}}", str(num_questions))
    prompt = prompt_template.replace("{{document}}", documents)


    body = {
        "prompt": prompt,
        "max_tokens_to_sample": 512,
        "temperature":0.9,
        "top_k":250,
        "top_p":1,
        "stop_sequences":["\n\nHuman:"]
    }

    response = bedrock_runtime.invoke_model(
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
        modelId=model_id
    )

    output = json.loads(response['body'].read())
    result = output['completion']
    q_pos = [(a.start(), a.end()) for a in list(re.finditer("Question:", result))]
    a_pos = [(a.start(), a.end()) for a in list(re.finditer("Answer:", result))]

    data_samples = {}
    questions = []
    answers = []

    for idx, q in enumerate(q_pos):
        q_start = q[1]
        a_start = a_pos[idx][0]
        a_end = a_pos[idx][1]
        question = result[q_start:a_start-1]
        if idx == len(q_pos) - 1:
            answer = result[a_end:]
        else:
            next_q_start = q_pos[idx+1][0]
            answer = result[a_end:next_q_start-2]
        questions.append(question)
        answers.append(answer)

    data_samples['question'] = questions
    data_samples['ground_truth'] = answers
    return data_samples

def generate_context_answers(bedrock_runtime, agent_runtime, model_id, knowledge_base_id, topk, questions):
    contexts = []
    answers = []
    
    for question in questions:
        response = agent_runtime.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={
                'text': question
            },
            retrievalConfiguration={
                'vectorSearchConfiguration': {
                    'numberOfResults': topk
                }
            }
        )
        
        retrieval_results = response['retrievalResults']
        local_contexts = []
        for result in retrieval_results:
            local_contexts.append(result['content']['text'])
        contexts.append(local_contexts)
        combined_docs = "\n".join(local_contexts)
        prompt = llm_prompt_template.replace("{{documents}}", combined_docs)
        prompt = prompt.replace("{{query}}", question)
        body = {
            "prompt": prompt,
            "max_tokens_to_sample": 512,
            "temperature":0.9,
            "top_k":250,
            "top_p":1,
            "stop_sequences":["\n\nHuman:"]
        }

        response = bedrock_runtime.invoke_model(
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
            modelId=model_id
        )
        output_bytes = response['body'].read()
        output = json.loads(output_bytes.decode("utf-8"))['completion']
        answers.append(output)
        
    return contexts, answers    


class Prompt(ragas.llms.prompt.Prompt):
    def to_string(self) -> str:
        """
        Generate the prompt string from the variables.
        """
        prompt_str = self.instruction + "\n"

        if self.examples:
            # Format the examples to match the Langchain prompt template
            for example in self.examples:
                prompt_str += "<example>"
                for key, value in example.items():
                    value = (
                        json.dumps(value, ensure_ascii=False).encode("utf8").decode()
                    )
                    value = (
                        value.replace("{", "{{").replace("}", "}}")
                        if self.output_type.lower() == "json"
                        else value
                    )
                    prompt_str += f"\n{key}: {value}"
                prompt_str += "\n"
                prompt_str += "</example>"
                prompt_str += "\n"
        if self.input_keys:
            prompt_str += "".join(f"\n{key}: {{{key}}}" for key in self.input_keys)

        return prompt_str

class AnswerCorrectnessPrompt(ragas.llms.prompt.Prompt):
    def to_string(self) -> str:
        """
        Generate the prompt string from the variables.
        """
        prompt_str = self.instruction + "\n"

        if self.examples:
            # Format the examples to match the Langchain prompt template
            for example in self.examples:
                prompt_str += "<example>"
                for key, value in example.items():
                    value = (
                        json.dumps(value, ensure_ascii=False).encode("utf8").decode()
                    )
                    value = (
                        value.replace("{", "{{").replace("}", "}}")
                        if self.output_type.lower() == "json"
                        else value
                    )
                    prompt_str += f"\n{key}: {value}"
                prompt_str += "\n"
                prompt_str += "</example>"
                prompt_str += "\n"
        # prompt_str += "Assistant:"
        prompt_str += "\nYour answer must be in JSON format, do not add any other statements in the response."
        if self.input_keys:
            prompt_str += "".join(f"\n{key}: {{{key}}}" for key in self.input_keys)

        if self.output_key:
            prompt_str += f"\n{self.output_key}:"

        return prompt_str
        
def two_stage_retrieval(agent_runtime, knowledge_base_id, input_query, retrieval_topk, reranking_model, rerank_top_k=2):
    # first stage: Retrieving the context.
    response = agent_runtime.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={
            'text': input_query
        },
        retrievalConfiguration={
            'vectorSearchConfiguration': {
                'numberOfResults': retrieval_topk
            }
        }
    )

    retrieval_results = response['retrievalResults']
    input_texts = [ x['content']['text'] for x in retrieval_results ]
    
    # second stage: Rerank the query / context pair
    documents = []
    response = reranking_model.predict({ "query": input_query, "documents" : input_texts, "topk" : rerank_top_k })
    print(f"response: {response}")
    for hit in response:
            documents.append(input_texts[hit['index']])
    return documents


def generate_two_stage_context_answers(bedrock_runtime, agent_runtime, model_id, knowledge_base_id, retrieval_topk, reranking_model, questions, rerank_top_k):
    contexts = []
    answers = []
    predictor = Predictor(endpoint_name=reranking_model, serializer=JSONSerializer(), deserializer=JSONDeserializer())
    for question in questions:
        retrieval_results = two_stage_retrieval(agent_runtime, knowledge_base_id, question, retrieval_topk, predictor, rerank_top_k)
        local_contexts = []
        documents = []
        for result in retrieval_results:
            local_contexts.append(result)
            
        contexts.append(local_contexts)
        # combined_docs = "\n".join(documents)
        combined_docs = "\n".join(local_contexts)
        prompt = llm_prompt_template.replace("{{documents}}", combined_docs)
        prompt = prompt.replace("{{query}}", question)
        body = {
            "prompt": prompt,
            "max_tokens_to_sample": 512,
            "temperature":0.9,
            "top_k":250,
            "top_p":1,
            "stop_sequences":["\n\nHuman:"]
        }

        response = bedrock_runtime.invoke_model(
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
            modelId=model_id
        )
        output_bytes = response['body'].read()
        output = json.loads(output_bytes.decode("utf-8"))['completion']
        answers.append(output)
    return contexts, answers
    
    
QUESTION_GEN = Prompt(
    name="question_generation",
    instruction="""Generate a question for the given answer and Identify if answer is noncommittal. Here are a few examples contained in <example></example> XML tag:
""",
    examples=[
        {
            "answer": """Albert Einstein was born in Germany.""",
            "context": """Albert Einstein was a German-born theoretical physicist who is widely held to be one of the greatest and most influential scientists of all time""",
            "output": """{"question":"Where was Albert Einstein born?","noncommittal":false}""",
        },
        {
            "answer": """It can change its skin color based on the temperature of its environment.""",
            "context": """A recent scientific study has discovered a new species of frog in the Amazon rainforest that has the unique ability to change its skin color based on the temperature of its environment.""",
            "output": """{"question":"What unique ability does the newly discovered species of frog have?","noncommittal":false}""",
        },
        {
            "answer": """Everest""",
            "context": """The tallest mountain on Earth, measured from sea level, is a renowned peak located in the Himalayas.""",
            "output": """{"question":"What is the tallest mountain on Earth?","noncommittal":false}""",
        },
        {
            "answer": """I don't know about the  groundbreaking feature of the smartphone invented in 2023 as am unware of information beyond 2022. """,
            "context": """In 2023, a groundbreaking invention was announced: a smartphone with a battery life of one month, revolutionizing the way people use mobile technology.""",
            "output": """{"question":"What was the groundbreaking feature of the smartphone invented in 2023?", "noncommittal":true}""",
        },
    ],
    input_keys=["answer", "context"],
    output_key="output",
    output_type="json",
)

CORRECTNESS_PROMPT = AnswerCorrectnessPrompt(
    name="answer_correctness",
    instruction="""Given a question and a ground truth, extract any statements matching the following categories:
    
"statements that are present in both the answer and the ground truth"
"statements present in the answer but not found in the ground truth"
"relevant statements found in the ground truth but omitted in the answer"

Here are a few examples contained in <example></example> XML tag:

    """,
    examples=[
        {
            "question": """What powers the sun and what is its primary function?""",
            "answer": """The sun is powered by nuclear fission, similar to nuclear reactors on Earth, and its primary function is to provide light to the solar system.""",
            "ground_truth": """The sun is actually powered by nuclear fusion, not fission. In its core, hydrogen atoms fuse to form helium, releasing a tremendous amount of energy. This energy is what lights up the sun and provides heat and light, essential for life on Earth. The sun's light also plays a critical role in Earth's climate system and helps to drive the weather and ocean currents.""",
            "Extracted statements": [
                {
                    "statements that are present in both the answer and the ground truth": [
                        "The sun's primary function is to provide light"
                    ],
                    "statements present in the answer but not found in the ground truth": [
                        "The sun is powered by nuclear fission",
                        "similar to nuclear reactors on Earth",
                    ],
                    "relevant statements found in the ground truth but omitted in the answer": [
                        "The sun is powered by nuclear fusion, not fission",
                        "In its core, hydrogen atoms fuse to form helium, releasing a tremendous amount of energy",
                        "This energy provides heat and light, essential for life on Earth",
                        "The sun's light plays a critical role in Earth's climate system",
                        "The sun helps to drive the weather and ocean currents",
                    ],
                }
            ],
        },
        {
            "question": """What is the boiling point of water?""",
            "answer": """The boiling point of water is 100 degrees Celsius at sea level.""",
            "ground_truth": """The boiling point of water is 100 degrees Celsius (212 degrees Fahrenheit) at sea level, but it can change with altitude.""",
            "Extracted statements": [
                {
                    "statements that are present in both the answer and the ground truth": [
                        "The boiling point of water is 100 degrees Celsius at sea level"
                    ],
                    "statements present in the answer but not found in the ground truth": [],
                    "relevant statements found in the ground truth but omitted in the answer": [
                        "The boiling point can change with altitude",
                        "The boiling point of water is 212 degrees Fahrenheit at sea level",
                    ],
                }
            ],
        },
    ],
    input_keys=["question", "answer", "ground_truth"],
    output_key="Extracted statements",
    output_type="json",
)

@dataclass
class AnswerCorrectness(ragas.metrics._answer_correctness.AnswerCorrectness):
    correctness_prompt: Prompt = field(default_factory=lambda: CORRECTNESS_PROMPT)

@dataclass
class AnswerRelevancy(ragas.metrics._answer_relevance.AnswerRelevancy):
    question_generation: Prompt = field(default_factory=lambda: QUESTION_GEN)


def create_opensearch_serverless_collection(vector_store_name, 
                                            index_name, 
                                            encryption_policy_name, 
                                            network_policy_name, 
                                            access_policy_name):
    identity = boto3.client('sts').get_caller_identity()['Arn']
    
    aoss_client = boto3.client('opensearchserverless')
    
    security_policy = aoss_client.create_security_policy(
        name = encryption_policy_name,
        policy = json.dumps(
            {
                'Rules': [{'Resource': ['collection/' + vector_store_name],
                'ResourceType': 'collection'}],
                'AWSOwnedKey': True
            }),
        type = 'encryption'
    )
    
    network_policy = aoss_client.create_security_policy(
        name = network_policy_name,
        policy = json.dumps(
            [
                {'Rules': [{'Resource': ['collection/' + vector_store_name],
                'ResourceType': 'collection'}],
                'AllowFromPublic': True}
            ]),
        type = 'network'
    )
    
    collection = aoss_client.create_collection(name=vector_store_name,type='VECTORSEARCH')
    
    while True:
        status = aoss_client.list_collections(collectionFilters={'name':vector_store_name})['collectionSummaries'][0]['status']
        if status in ('ACTIVE', 'FAILED'): break
        time.sleep(10)
    
    access_policy = aoss_client.create_access_policy(
        name = access_policy_name,
        policy = json.dumps(
            [
                {
                    'Rules': [
                        {
                            'Resource': ['collection/' + vector_store_name],
                            'Permission': [
                                'aoss:CreateCollectionItems',
                                'aoss:DeleteCollectionItems',
                                'aoss:UpdateCollectionItems',
                                'aoss:DescribeCollectionItems'],
                            'ResourceType': 'collection'
                        },
                        {
                            'Resource': ['index/' + vector_store_name + '/*'],
                            'Permission': [
                                'aoss:CreateIndex',
                                'aoss:DeleteIndex',
                                'aoss:UpdateIndex',
                                'aoss:DescribeIndex',
                                'aoss:ReadDocument',
                                'aoss:WriteDocument'],
                            'ResourceType': 'index'
                        }],
                    'Principal': [identity],
                    'Description': 'Easy data policy'}
            ]),
        type = 'data'
    )
    collection_id = collection['createCollectionDetail']['id']
    collection_arn = collection['createCollectionDetail']['arn']
    host = collection['createCollectionDetail']['id'] + '.' + os.environ.get("AWS_DEFAULT_REGION", None) + '.aoss.amazonaws.com'
    return host, collection_id, collection_arn

def create_index(host, region, credentials, index_name, embedding_dim):
    service = 'aoss'
    auth = AWSV4SignerAuth(credentials, region, service)
    aoss_pyclient = OpenSearch(
        hosts = [{'host': host, 'port': 443}],
        http_auth = auth,
        use_ssl = True,
        verify_certs = True,
        connection_class = RequestsHttpConnection,
        pool_maxsize = 20
    )

    index_body = {
        "settings": {
            "index": {
                "knn": "true",
                "number_of_shards": "2",
                "knn.algo_param": {
                  "ef_search": "512"
                },
            }
        },
        "mappings": {
          "properties": {
            "AMAZON_BEDROCK_METADATA": {
              "type": "text",
              "index": "false"
            },
            "AMAZON_BEDROCK_TEXT_CHUNK": {
              "type": "text"
            },
            "bedrock-knowledge-base-default-vector": {
              "type": "knn_vector",
              "dimension": embedding_dim,
              "method": {
                "engine": "faiss",
                "name": "hnsw",
                "parameters": {}
              }
            }
          }
        }
    }
    response = aoss_pyclient.indices.create(index_name, body=index_body)
    return response

def create_knowledge_base_service_role(embedding_model_arn, collection_id, bucket, s3_prefix, role_name):
    sts_client = boto3.client('sts')
    response = sts_client.get_caller_identity()
    aws_account = response['Account']
    region = sts_client.meta.region_name

    iam = boto3.client('iam')
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AmazonBedrockKnowledgeBaseTrustPolicy",
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": aws_account
                    },
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{region}:{aws_account}:knowledge-base/*"
                    }
                }
            }
        ]
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockInvokeModelStatement",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel"
                ],
                "Resource": [
                    embedding_model_arn
                ]
            },
            {
                "Sid": "OpenSearchServerlessAPIAccessAllStatement",
                "Effect": "Allow",
                "Action": [
                    "aoss:APIAccessAll"
                ],
                "Resource": [
                    f"arn:aws:aoss:{region}:{aws_account}:collection/{collection_id}"
                ]
            },
            {
                "Sid": "S3ListBucketStatement",
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket"
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket}"
                ],
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceAccount": aws_account
                    }
                }
            },
            {
                "Sid": "S3GetObjectStatement",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject"
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/{s3_prefix}/*"
                ],
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceAccount": aws_account
                    }
                }
            }
        ]
    }
    assume_role_policy_document = json.dumps(assume_role_policy)
    response = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=assume_role_policy_document
    )
    role_arn = response['Role']['Arn']
    policy_document = json.dumps(policy)
    response = iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f'{role_name}-policy',
        PolicyDocument=policy_document
    )
    return role_arn

def update_opensearch_data_access_policy(access_policy_name, role_arn):

    aoss_client = boto3.client('opensearchserverless')
    response = aoss_client.get_access_policy(
        name=access_policy_name,
        type='data'
    )

    access_policy_detail = response['accessPolicyDetail']['policy'][0]
    access_policy_detail['Principal'].append(role_arn)
    response = aoss_client.update_access_policy(
        name=access_policy_name,
        policy=json.dumps(response['accessPolicyDetail']['policy']),
        policyVersion=response['accessPolicyDetail']['policyVersion'],
        type='data'
    )
    return response

def _create_knowledge_base(knowledge_base_name, role_arn, embedding_model_arn, collection_arn, index_name, bucket, s3_prefix):
    bedrock_agent = boto3.client("bedrock-agent")
    response = bedrock_agent.create_knowledge_base(
        name=knowledge_base_name,
        description='Knowledge Base for Bedrock',
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            'type': 'VECTOR',
            'vectorKnowledgeBaseConfiguration': {
                'embeddingModelArn': embedding_model_arn
            }
        },
        storageConfiguration={
            'type': 'OPENSEARCH_SERVERLESS',
            'opensearchServerlessConfiguration': {
                'collectionArn': collection_arn,
                'vectorIndexName': index_name,
                'fieldMapping': {
                    'vectorField':  "bedrock-knowledge-base-default-vector",
                    'textField': 'AMAZON_BEDROCK_TEXT_CHUNK',
                    'metadataField': 'AMAZON_BEDROCK_METADATA'
                }
            }
        }
    )
    knowledge_base_id = response['knowledgeBase']['knowledgeBaseId']
    knowledge_base_name = response['knowledgeBase']['name']

    response = bedrock_agent.create_data_source(
    knowledgeBaseId=knowledge_base_id,
    name=f"{knowledge_base_name}-ds",
    dataSourceConfiguration={
        'type': 'S3',
        's3Configuration': {
            'bucketArn': f"arn:aws:s3:::{bucket}",
            'inclusionPrefixes': [
                f"{s3_prefix}/",
            ]
        }
    },
    vectorIngestionConfiguration={
            'chunkingConfiguration': {
                'chunkingStrategy': 'FIXED_SIZE',
                'fixedSizeChunkingConfiguration': {
                    'maxTokens': 300,
                    'overlapPercentage': 10
                }
            }
        }
    )
    data_source_id = response['dataSource']['dataSourceId']
    
    # Check status to make sure the KB is created before we could start the ingestion job.
    kb_status = bedrock_agent.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
    while kb_status not in [ "ACTIVE", "FAILED", "DELETE_UNSUCCESSFUL" ]:
        response = bedrock_agent.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
        kb_status = response['knowledgeBase']['status']

    if kb_status != "ACTIVE":
        raise Exception("Bedrock Knowledgebase did not create successfully. Please check for error before proceeding") 
    
    print(f"Bedrock KnowledgeBase {knowledge_base_id} created successfully")
    response = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
    )

    ingestion_job_id = response['ingestionJob']['ingestionJobId']
    ingestion_job_status = response['ingestionJob']['status']

    while ingestion_job_status not in ['COMPLETE', 'FAILED']:
        response = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=ingestion_job_id
        )
        ingestion_job_status = response['ingestionJob']['status']
        time.sleep(5)
    return knowledge_base_id


def create_knowledge_base(knowledge_base_name, kb_role_name, embedding_model_arn, embedding_dim, s3_bucket, s3_prefix, vector_store_name, index_name, encryption_policy_name, network_policy_name, access_policy_name, region, credentials):
    host, collection_id, collection_arn = create_opensearch_serverless_collection(vector_store_name,
                                                                                  index_name,
                                                                                  encryption_policy_name,
                                                                                  network_policy_name,
                                                                                  access_policy_name)
    time.sleep(180) # sleeps for a while for the collection to be fully created
    response = create_index(host, region, credentials, index_name, embedding_dim)
    role_arn = create_knowledge_base_service_role(embedding_model_arn, collection_id, s3_bucket, s3_prefix, kb_role_name)
    response = update_opensearch_data_access_policy(access_policy_name, role_arn)
    time.sleep(60) # sleeps until the IAM role is created successfully
    knowledge_base_id = _create_knowledge_base(knowledge_base_name, role_arn, embedding_model_arn, collection_arn, index_name, s3_bucket, s3_prefix)
    return knowledge_base_id
    
    
    