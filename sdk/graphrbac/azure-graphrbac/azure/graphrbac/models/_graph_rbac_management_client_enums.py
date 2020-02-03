# coding=utf-8
# --------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# Code generated by Microsoft (R) AutoRest Code Generator.
# Changes may cause incorrect behavior and will be lost if the code is regenerated.
# --------------------------------------------------------------------------

from enum import Enum

class GroupMembershipClaimTypes(str, Enum):
    
    none = "None"  #: The value 'undefined'.
    security_group = "SecurityGroup"  #: The value 'undefined'.
    all = "All"  #: The value 'undefined'.

class UserType(str, Enum):
    
    member = "Member"  #: The value 'undefined'.
    guest = "Guest"  #: The value 'undefined'.

class ConsentType(str, Enum):
    
    all_principals = "AllPrincipals"  #: The value 'undefined'.
    principal = "Principal"  #: The value 'undefined'.