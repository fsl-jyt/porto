#include <sstream>

#include "value.hpp"
#include "util/log.hpp"
#include "util/string.hpp"

void TValue::ExpectType(EValueType type) {
    if (type != Type)
        PORTO_RUNTIME_ERROR("Invalid " + Name + " type: " +
                            std::to_string((int)Type) + " != " +
                            std::to_string((int)type));
}

bool TValue::NeedDefault() {
    return !(Flags & NODEF_VALUE);
}

std::string StringToString(const std::string &v) {
    return v;
}

std::string TStringValue::GetDefaultString(std::shared_ptr<TContainer> c) {
    return "";
}

TError TStringValue::SetString(std::shared_ptr<TContainer> c,
                               std::shared_ptr<TVariant> v,
                               const std::string &value) {
    return v->Set(Type, value);
}

std::string TStringValue::GetString(std::shared_ptr<TContainer> c,
                                    std::shared_ptr<TVariant> v) {
    if (!v->HasValue() && NeedDefault())
        return GetDefaultString(c);

    return v->Get<std::string>(Type);
}

#define DEFINE_VALUE_GETTER(NAME, TYPE) \
    std::string T ## NAME ## Value::GetDefaultString(std::shared_ptr<TContainer> c) { \
        return NAME ## ToString(GetDefault ## NAME(c)); \
    } \
    std::string T ## NAME ## Value:: GetString(std::shared_ptr<TContainer> c, \
                                               std::shared_ptr<TVariant> v) { \
        if (!v->HasValue() && NeedDefault()) \
            return NAME ## ToString(GetDefault ## NAME(c)); \
        else \
            return NAME ## ToString(Get ## NAME(c, v)); \
    }

DEFINE_VALUE_GETTER(Bool, bool)

std::string BoolToString(const bool &v) {
    if (v)
        return "true";
    else
        return "false";
}

TError TBoolValue::SetString(std::shared_ptr<TContainer> c,
                         std::shared_ptr<TVariant> v,
                         const std::string &value) {
    if (value != "true" && value != "false")
        return TError(EError::InvalidValue, "invalid boolean value");

    bool tmp = value == "true";

    return SetBool(c, v, tmp);
}

DEFINE_VALUE_GETTER(Int, int)

std::string IntToString(const int &v) {
    return std::to_string(v);
}

TError TIntValue::SetString(std::shared_ptr<TContainer> c,
                            std::shared_ptr<TVariant> v,
                            const std::string &value) {
    int tmp;
    TError error = StringToInt(value, tmp);
    if (error)
        return TError(EError::InvalidValue, "Invalid integer value " + value);

    return SetInt(c, v, tmp);
}

DEFINE_VALUE_GETTER(Uint, uint64_t)

std::string UintToString(const uint64_t &v) {
    return std::to_string(v);
}

TError TUintValue::SetString(std::shared_ptr<TContainer> c,
                             std::shared_ptr<TVariant> v,
                             const std::string &value) {
    uint64_t tmp;
    TError error = StringToUint64(value, tmp);
    if (error)
        return TError(EError::InvalidValue, "Invalid unsigned integer value " + value);

    return SetUint(c, v, tmp);
}

DEFINE_VALUE_GETTER(Map, TUintMap)

std::string MapToString(const TUintMap &v) {
    std::stringstream str;

    for (auto &kv : v) {
        if (str.str().length())
            str << "; ";
        str << kv.first << ": " << kv.second;
    }

    return str.str();
}

TError TMapValue::SetString(std::shared_ptr<TContainer> c,
                            std::shared_ptr<TVariant> v,
                            const std::string &value) {
    TUintMap m;
    std::vector<std::string> lines;
    TError error = SplitEscapedString(value, ';', lines);
    if (error)
        return error;

    for (auto &line : lines) {
        std::vector<std::string> nameval;

        (void)SplitEscapedString(line, ':', nameval);
        if (nameval.size() != 2)
            return TError(EError::InvalidValue, "Invalid format");

        std::string key = StringTrim(nameval[0]);
        uint64_t val;

        error = StringToUint64(nameval[1], val);
        if (error)
            return TError(EError::InvalidValue, "Invalid value " + nameval[1]);

        m[key] = val;
    }

    return SetMap(c, v, m);
}

DEFINE_VALUE_GETTER(List, TStrList)

std::string ListToString(const TStrList &v) {
    std::stringstream str;

    for (auto &val : v) {
        if (str.str().length())
            str << "; ";
        str << val;
    }

    return str.str();
}

TError TListValue::SetString(std::shared_ptr<TContainer> c,
                             std::shared_ptr<TVariant> v,
                             const std::string &value) {
    std::vector<std::string> vec;
    TStrList m;
    TError error = SplitEscapedString(value, ';', vec);
    if (error)
        return error;

    for (auto &val : vec) {
        std::string tmp = StringTrim(val);
        if (!tmp.length())
            continue;
        m.push_back(tmp);
    }

    return SetList(c, v, m);
}

TError TValueSet::Register(TValue *p) {
    if (Value.find(p->Name) != Value.end())
        return TError(EError::Unknown, "Invalid " + p->Name + " definition");
    Value[p->Name] = p;
    return TError::Success();
}

TError TValueSet::Register(const std::vector<TValue *> &v) {
    for (auto &p : v) {
        TError error = Register(p);
        if (error)
            return error;
    }
    return TError::Success();
}

bool TValueSet::Valid(const std::string &name) {
    return Value.find(name) != Value.end();
}

TValue *TValueSet::Get(const std::string &name) {
    return Value[name];
}

std::vector<std::string> TValueSet::GetNames() {
    std::vector<std::string> v;
    for (auto kv: Value)
        v.push_back(kv.first);
    return v;
}

TError TVariantSet::Get(const std::string &name,
                        std::shared_ptr<TContainer> &c, TValue **p,
                        std::shared_ptr<TVariant> &v) {
    c = Container.lock();
    if (!c)
        return TError(EError::Unknown, "Can't convert weak container reference");

    *p = nullptr;
    if (Variant.find(name) == Variant.end()) {
        if (!ValueSet->Valid(name))
            return TError(EError::Unknown, "Invalid value " + name);
        *p = ValueSet->Get(name);
        v = Variant[name] = std::make_shared<TVariant>((*p)->Type, name);
    } else {
        *p = ValueSet->Get(name);
        v = Variant[name];
    }

    return TError::Success();
}

std::vector<std::string> TVariantSet::List() {
    std::vector<std::string> v;
    for (auto kv : Variant)
        v.push_back(kv.first);
    return v;
}

bool TVariantSet::IsDefault(const std::string &name) {
    if (Variant.find(name) == Variant.end())
        return true;

    TValue *p = nullptr;
    std::shared_ptr<TContainer> c;
    std::shared_ptr<TVariant> v;
    TError error = Get(name, c, &p, v);
    if (error) {
        TLogger::LogError(error, "Can't check whether " + name + " is default");
        return false;
    }

    return p->IsDefault(c, v);
}
