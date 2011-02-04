//===--- sizeof_reference-i1.h - test input file for iwyu -----------------===//
//
//                     The LLVM Compiler Infrastructure
//
// This file is distributed under the University of Illinois Open Source
// License. See LICENSE.TXT for details.
//
//===----------------------------------------------------------------------===//

#ifndef DEVTOOLS_MAINTENANCE_INCLUDE_WHAT_YOU_USE_TESTS_SIZEOF_REFERENCE_I1_H_
#define DEVTOOLS_MAINTENANCE_INCLUDE_WHAT_YOU_USE_TESTS_SIZEOF_REFERENCE_I1_H_

template <typename T> struct IndirectTemplateStruct {
  T value;   // require full type information for t;
};

template <typename T> struct SizeofTakingStruct {
  char value[sizeof(T)];
};

#endif  // DEVTOOLS_MAINTENANCE_INCLUDE_WHAT_YOU_USE_TESTS_SIZEOF_REFERENCE_I1_H_
